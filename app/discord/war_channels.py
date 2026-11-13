"""Creation and upkeep of the per-war Discord workspace (spec §14, §30, §31, §37).

* :class:`WarChannelManager` — find-or-create the war category + 4 channels.
  State lives in ``wars.discord_category_id`` and the ``discord_channels`` table;
  a channel that was manually deleted is recreated. A server's category and
  channels are **reused by each new war** rather than created afresh, so a
  permission lock the owner set on them (bot-only channels) carries over — the
  bot cannot set that lock itself.
  If the server has run ``/setup channel``, every purpose maps to that single
  existing channel instead — no category, no per-war channels.
  If the server has run ``/setup board`` in channels its admins made, each board
  goes to its own chosen channel and nothing is ever created — so the bot can run
  without Manage Channels (Faction A, Oct 2026).
* :class:`WarBoard` — post a channel's live embed the first time, then edit that
  same message on every refresh instead of posting a new one.

Both depend on :class:`app.discord.gateway.GuildGateway`, so the logic is unit
tested without a live Discord. Requires the bot's **Manage Channels** permission
and nothing more privileged (single-channel mode needs even less — it never
creates anything — but the permission is still requested since most servers
use the default per-war layout). Per-war mode without it never tries a create:
boards stay in whatever channel they already had, and a refused create backs off
for ``_CREATE_RETRY_SECONDS``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace

import discord

from app.database.database import Database
from app.database.models import DiscordChannel, War, WarLogEvent
from app.database.repositories import (
    DiscordChannelRepository,
    OverdoseRepository,
    WarHitRepository,
    WarLogRepository,
    WarRepository,
    message_ids_of,
)
from app.discord.embeds import war_log_event_embed
from app.discord.gateway import GatewayNotReady, GuildGateway
from app.discord.hit_embeds import build_hit_messages
from app.discord.summary_embeds import war_summary_messages
from app.utils.formatting import truncate
from app.utils.logging import get_logger
from app.utils.time import utcnow
from app.war.hit_summary import hits_csv, summarise_hits, summary_csv, summary_filename
from app.war.warlog import WAR_SUMMARY_KIND

logger = get_logger(__name__)

# (purpose, channel name, channel topic). Purpose is the stable key used in the
# discord_channels table and by WarBoard.
CHANNEL_LAYOUT: tuple[tuple[str, str, str], ...] = (
    ("information", "📢-war-information", "Live war summary. Auto-updated."),
    ("stats", "📊-enemy-stats", "Enemy roster ranked by estimated strength. Auto-updated."),
    ("targets", "⚔️-target-board", "Live target list: attack now, out of hospital, landing."),
    ("log", "📝-war-log", "Meaningful war events only."),
    ("activity", "🕒-enemy-activity", "When the enemy is busy and quiet (TCT). Every 15 min."),
)
# The live-score box is its own message inside the information channel, so it has a
# ``discord_channels`` row of its own (one live message id per row) but is deliberately
# NOT in CHANNEL_LAYOUT — that would create a fifth channel for it.
SCORE_PURPOSE = "score"
_PURPOSES = (*(p for p, _, _ in CHANNEL_LAYOUT), SCORE_PURPOSE)

CATEGORY_PREFIX = "⚔️ WAR —"

# war row column that mirrors each channel id, for cheap reads by /war etc.
_WAR_COLUMN = {
    "information": "discord_information_channel_id",
    "stats": "discord_stats_channel_id",
    "targets": "discord_targets_channel_id",
    "log": "discord_log_channel_id",
}


@dataclass(frozen=True)
class WarWorkspace:
    category_id: int
    channel_ids: dict[str, int]  # purpose -> channel id


# After Discord refuses a channel/category create, wait this long before trying again.
_CREATE_RETRY_SECONDS = 600.0


def _category_name(enemy_faction_name: str | None) -> str:
    name = (enemy_faction_name or "Unknown Faction").replace("\n", " ").strip()
    return truncate(f"{CATEGORY_PREFIX} {name}", 100)


class WarChannelManager:
    def __init__(
        self,
        database: Database,
        gateway: GuildGateway,
        *,
        single_channel_id: int | None = None,
        board_channels: Mapping[str, int] | None = None,
    ) -> None:
        self._db = database
        self._gateway = gateway
        self._single_channel_id = single_channel_id
        self._board_channels = dict(board_channels or {})
        # Without these, a server that took away Manage Channels got a forbidden create
        # (403 / 50013) every 15s tick and a warning each time (Faction A, 1 Oct 2026).
        self._create_blocked_until = 0.0
        self._warned_no_permission = False

    async def ensure_workspace(self, war_row_id: int) -> WarWorkspace:
        if self._board_channels:
            return await self._ensure_own_channels_workspace(war_row_id)
        if self._single_channel_id is not None:
            return await self._ensure_single_channel_workspace(war_row_id)

        async with self._db.session() as session:
            wars = WarRepository(session)
            channels = DiscordChannelRepository(session)

            war = await wars.get_by_id(war_row_id)
            if war is None:
                raise ValueError(f"war row {war_row_id} not found")

            may_create = await self._may_create_channels(war_row_id)

            # 1. Category — reuse this server's earlier one if it still exists.
            reusable: dict[str, int] = {}
            category_id = war.discord_category_id
            if category_id is None or not await self._gateway.category_exists(category_id):
                category_id, reusable = await self._find_reusable(wars, channels, war)
                if category_id is not None:
                    await self._gateway.rename_category(
                        category_id, _category_name(war.enemy_faction_name)
                    )
                    logger.info(
                        "war.category_reused", war_row_id=war_row_id, category_id=category_id
                    )
                elif may_create:
                    try:
                        category_id = await self._gateway.create_category(
                            _category_name(war.enemy_faction_name)
                        )
                        logger.info(
                            "war.category_created", war_row_id=war_row_id, category_id=category_id
                        )
                    except discord.Forbidden as exc:
                        may_create = self._creation_refused(war_row_id, exc)
                if category_id is not None:
                    war.discord_category_id = category_id

            # 2. The five channels.
            existing = {row.purpose: row for row in await channels.for_war(war_row_id)}
            # A channel held by 2+ purposes is a leftover `/setup channel` room (cleared
            # mid-war); per-war mode gives each purpose its own channel, so don't reuse it.
            # The score box lives in #war-information by design, so it is not counted —
            # counting it made every refresh spawn another #war-information channel.
            owners: dict[int, int] = {}
            for r in existing.values():
                if r.purpose != SCORE_PURPOSE:
                    owners[r.channel_id] = owners.get(r.channel_id, 0) + 1
            channel_ids: dict[str, int] = {}
            for purpose, name, topic in CHANNEL_LAYOUT:
                row = existing.get(purpose)
                row_usable = row is not None and await self._gateway.channel_exists(row.channel_id)
                if row is not None and row_usable and owners[row.channel_id] == 1:
                    channel_ids[purpose] = row.channel_id
                    continue
                earlier = reusable.get(purpose)
                new_id: int | None = None
                if earlier is not None and await self._gateway.channel_exists(earlier):
                    new_id = earlier
                    event = "war.channel_reused"
                elif may_create and category_id is not None:
                    try:
                        new_id = await self._gateway.create_text_channel(
                            name, category_id=category_id, topic=topic
                        )
                        event = "war.channel_created"
                    except discord.Forbidden as exc:
                        may_create = self._creation_refused(war_row_id, exc)
                if new_id is None:
                    # Can't give it its own channel: keep posting where it was (e.g. the
                    # old shared /setup channel room) rather than going dark. Nowhere to
                    # post -> the board is skipped until an admin fixes it.
                    if row is not None and row_usable:
                        channel_ids[purpose] = row.channel_id
                    continue
                await channels.upsert_channel(war_row_id, purpose, new_id)
                if purpose in _WAR_COLUMN:  # newer channels have no mirror column on wars
                    setattr(war, _WAR_COLUMN[purpose], new_id)
                channel_ids[purpose] = new_id
                logger.info(event, war_row_id=war_row_id, purpose=purpose, channel_id=new_id)

        return WarWorkspace(category_id=category_id or 0, channel_ids=channel_ids)

    async def _may_create_channels(self, war_row_id: int) -> bool:
        """False while backing off after a refused create, or when the bot plainly
        lacks Manage Channels — checked from cache so no doomed API call is made.
        Warns once per spell without the permission, not every tick."""
        if time.monotonic() < self._create_blocked_until:
            return False
        if await self._gateway.can_manage_channels():
            self._warned_no_permission = False
            return True
        if not self._warned_no_permission:
            self._warned_no_permission = True
            logger.warning(
                "war.manage_channels_missing",
                war_row_id=war_row_id,
                hint="give the bot Manage Channels, or run /setup board in admin-made channels",
            )
        return False

    def _creation_refused(self, war_row_id: int, exc: discord.Forbidden) -> bool:
        """Discord refused a create despite the permission check (e.g. a category
        overwrite). Stop trying for a while; returns the new ``may_create`` (False)."""
        self._create_blocked_until = time.monotonic() + _CREATE_RETRY_SECONDS
        logger.warning(
            "war.channel_create_forbidden",
            war_row_id=war_row_id,
            retry_in_seconds=_CREATE_RETRY_SECONDS,
            detail=str(exc),
        )
        return False

    async def _find_reusable(
        self, wars: WarRepository, channels: DiscordChannelRepository, war: War
    ) -> tuple[int | None, dict[str, int]]:
        """The category (and its ``purpose -> channel id`` map) of this server's most
        recent earlier war whose category still exists, else ``(None, {})``.

        Anchored on wars that *had* a category, so a server that once used
        ``/setup channel`` cannot have its one shared channel adopted as, say, all
        four of the per-war channels."""
        for earlier_war_id, category_id in await wars.previous_workspaces(
            war.guild_id, exclude_war_id=war.id
        ):
            if await self._gateway.category_exists(category_id):
                rows = await channels.for_war(earlier_war_id)
                return category_id, {row.purpose: row.channel_id for row in rows}
        return None, {}

    async def _ensure_own_channels_workspace(self, war_row_id: int) -> WarWorkspace:
        """Each board goes to the channel ``/setup board`` gave it. A board not given
        one yet falls back to the ``/setup channel`` room if there is one, else it is
        not posted. Never creates anything — that is the point of this mode."""
        channel_ids: dict[str, int] = {}
        async with self._db.session() as session:
            channels = DiscordChannelRepository(session)
            for purpose, _, _ in CHANNEL_LAYOUT:
                channel_id = self._board_channels.get(purpose, self._single_channel_id)
                if channel_id is None:
                    continue
                if not await self._gateway.channel_exists(channel_id):
                    # Deleted, or the bot lost View Channel. Leave the row alone and
                    # carry on with the other boards; an admin re-runs /setup board.
                    logger.warning(
                        "war.board_channel_missing",
                        war_row_id=war_row_id,
                        purpose=purpose,
                        channel_id=channel_id,
                    )
                    continue
                await channels.upsert_channel(war_row_id, purpose, channel_id)
                channel_ids[purpose] = channel_id

        return WarWorkspace(
            category_id=0,  # no category in this mode; nothing reads it
            channel_ids=channel_ids,
        )

    async def _ensure_single_channel_workspace(self, war_row_id: int) -> WarWorkspace:
        """Map every purpose to the one server-designated channel (``/setup
        channel``) instead of creating a category + per-war channels."""
        channel_id = self._single_channel_id
        assert channel_id is not None
        if not await self._gateway.channel_exists(channel_id):
            raise GatewayNotReady(f"configured war-room channel {channel_id} not available")

        async with self._db.session() as session:
            channels = DiscordChannelRepository(session)
            for purpose, _, _ in CHANNEL_LAYOUT:
                await channels.upsert_channel(war_row_id, purpose, channel_id)

        return WarWorkspace(
            category_id=channel_id,  # unused in single-channel mode; not a real category
            channel_ids={purpose: channel_id for purpose, _, _ in CHANNEL_LAYOUT},
        )


class WarBoard:
    """Posts / edits the single live embed per channel purpose (spec §31)."""

    def __init__(self, database: Database, gateway: GuildGateway) -> None:
        self._db = database
        self._gateway = gateway
        # (channel_id, message_id) -> (fingerprint of what we last put there, when).
        # Boards refresh every 15s but often don't change; re-sending an identical edit
        # still costs a Discord call, and in single-channel mode all boards share one
        # channel's edit bucket — that is what caused the 429s and skipped refreshes.
        self._last_edit: dict[tuple[int, int], tuple[str, float]] = {}

    async def publish(
        self,
        war_row_id: int,
        purpose: str,
        embed: discord.Embed | list[discord.Embed],
        *,
        view: discord.ui.View | None = None,
    ) -> bool:
        """True once the embed is on Discord; False if it was skipped or deferred
        (no channel row yet / gateway not ready), so a caller can retry later.
        ``view`` (drop-downs under the message) is left untouched when None."""
        embeds = embed if isinstance(embed, list) else [embed]
        return await self.publish_messages(war_row_id, purpose, [embeds], view=view)

    async def publish_messages(
        self,
        war_row_id: int,
        purpose: str,
        messages: list[list[discord.Embed]],
        *,
        view: discord.ui.View | None = None,
    ) -> bool:
        """Like ``publish``, for a board spread over several messages (``messages[i]``
        is message i's embeds), kept in that order. ``view`` goes under the LAST message
        only. Messages no longer needed are deleted, so a board that shrinks back to one
        message leaves no stale part 2 behind."""
        if purpose not in _PURPOSES:
            raise ValueError(f"unknown channel purpose {purpose!r}")
        if not messages:
            raise ValueError("publish_messages needs at least one message")

        async with self._db.session() as session:
            row: DiscordChannel | None = await DiscordChannelRepository(session).get(
                war_row_id, purpose
            )
            if row is None:
                logger.warning(
                    "war.publish_skipped_no_channel", war_row_id=war_row_id, purpose=purpose
                )
                return False
            channel_id = row.channel_id
            old_ids = message_ids_of(row)

        new_ids: list[int] = []
        # Once one part is posted afresh it lands at the bottom of the channel, so every
        # part after it must be posted afresh too, or the board would read 2 then 1.
        reposted = False
        done = False
        try:
            for i, embeds in enumerate(messages):
                part_view = view
                if view is not None and i < len(messages) - 1:
                    # Empty view: takes away a drop-down left from when this was the last part.
                    part_view = discord.ui.View(timeout=None)
                old = old_ids[i] if i < len(old_ids) else None
                if old is not None and not reposted:
                    if await self._edit(channel_id, old, embeds, part_view):
                        new_ids.append(old)
                        continue
                    old = None  # gone (deleted by hand): nothing to remove
                if old is not None:
                    await self._gateway.remove_message(channel_id, old)
                new_id = await self._gateway.send_embeds(channel_id, embeds, view=part_view)
                self._last_edit[(channel_id, new_id)] = (
                    _fingerprint(embeds, part_view),
                    time.monotonic(),
                )
                new_ids.append(new_id)
                reposted = True
            for leftover in old_ids[len(messages) :]:
                await self._gateway.remove_message(channel_id, leftover)
            done = True
        except GatewayNotReady:
            logger.info("war.publish_deferred", war_row_id=war_row_id, purpose=purpose)
            return False
        finally:
            # Saved even after a failure part-way, so a part already posted is edited on
            # the next refresh instead of being posted again as a duplicate.
            ids = new_ids if done else new_ids + old_ids[len(new_ids) :]
            if ids != old_ids:
                async with self._db.session() as session:
                    await DiscordChannelRepository(session).set_live_messages(
                        war_row_id, purpose, ids
                    )
        return True

    async def _edit(
        self,
        channel_id: int,
        message_id: int,
        embeds: list[discord.Embed],
        view: discord.ui.View | None,
    ) -> bool:
        """Edit in place (skipped when nothing changed lately); False if it's gone."""
        fingerprint = _fingerprint(embeds, view)
        now = time.monotonic()
        last = self._last_edit.get((channel_id, message_id))
        # Still re-edit now and then so a board someone deleted by hand gets reposted.
        if (
            last is not None
            and last[0] == fingerprint
            and now - last[1] < _UNCHANGED_RECHECK_SECONDS
        ):
            return True
        if not await self._gateway.edit_embeds(channel_id, message_id, embeds, view=view):
            return False
        self._last_edit[(channel_id, message_id)] = (fingerprint, now)
        return True


_UNCHANGED_RECHECK_SECONDS = 300.0


def _fingerprint(embeds: list[discord.Embed], view: discord.ui.View | None) -> str:
    payload = {
        "embeds": [e.to_dict() for e in embeds],
        "view": view.to_components() if view is not None else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# Hits fetched per posting pass. A whole-war backlog is worked off over several
# passes rather than one long burst that would trip Discord's send rate limit.
_HITS_PER_PASS = 400

# Stored as ``posted_message_id`` for a war-log event or hit that Discord rejected
# outright (a 400 — e.g. an embed over its size limits). Not a real message id
# (Discord ids are never 0); it only needs to be non-NULL so the row stops being
# "pending" and no longer blocks everything queued behind it.
REJECTED_MESSAGE_ID = 0


def _rejected_message(exc: discord.HTTPException) -> bool:
    """True when Discord refused *this message* and a retry can never succeed.

    403/404 are about the channel (permission removed, channel deleted) and 429/5xx
    are transient — in those cases every queued message would fail the same way, so
    the caller stops and retries next pass instead of dropping anything.
    """
    if isinstance(exc, discord.Forbidden | discord.NotFound):
        return False
    return 400 <= exc.status < 500 and exc.status != 429


@dataclass(frozen=True)
class _Outgoing:
    """One message to send: its embeds plus any ``(filename, bytes)`` files."""

    embeds: list[discord.Embed]
    attachments: tuple[tuple[str, bytes], ...] = ()


class WarLogBoard:
    """Posts each war-log event **once** — append-only, unlike ``WarBoard``'s
    edit-in-place (spec §32). Order is preserved; a gateway, permission or Discord
    outage failure stops the batch so nothing is skipped, and unposted events are
    retried next call. A message Discord rejects outright (400) is marked
    ``REJECTED_MESSAGE_ID`` and skipped so it cannot block the queue forever."""

    def __init__(self, database: Database, gateway: GuildGateway) -> None:
        self._db = database
        self._gateway = gateway
        # The war-detect and refresh jobs can overlap; without this both would
        # read the same unposted hits and post them twice.
        self._hits_lock = asyncio.Lock()
        self._events_lock = asyncio.Lock()  # likewise, so the scoreboard posts once
        # (feed, war) pairs whose channel is currently refusing sends, so a lost
        # permission is logged once rather than on every pass until it is fixed.
        self._blocked: set[tuple[str, int]] = set()

    def _send_ok(self, feed: str, war_row_id: int) -> None:
        self._blocked.discard((feed, war_row_id))

    def _send_blocked(self, feed: str, war_row_id: int, exc: discord.HTTPException) -> None:
        if (feed, war_row_id) in self._blocked:
            return
        self._blocked.add((feed, war_row_id))
        logger.warning(
            "war.log_send_blocked",
            feed=feed,
            war_row_id=war_row_id,
            status=exc.status,
            detail=str(exc),
            note="nothing dropped; retried every pass until Discord accepts it",
        )

    async def publish_pending_hits(self, war_row_id: int) -> int:
        """Post our faction's not-yet-posted hits, colour-coded (war vs outside)."""
        async with self._hits_lock:
            async with self._db.session() as session:
                channel = await DiscordChannelRepository(session).get(war_row_id, "log")
                hits = await WarHitRepository(session).unposted_outgoing(
                    war_row_id, limit=_HITS_PER_PASS
                )

            if channel is None or not hits:
                return 0

            posted = 0
            for message in build_hit_messages(hits):
                try:
                    message_id = await self._gateway.send_embeds(channel.channel_id, message.embeds)
                except GatewayNotReady:
                    logger.info("war.hits_publish_deferred", war_row_id=war_row_id)
                    break
                except discord.HTTPException as exc:
                    if not _rejected_message(exc):
                        self._send_blocked("hits", war_row_id, exc)
                        break
                    logger.error(
                        "war.hits_message_rejected",
                        war_row_id=war_row_id,
                        hit_ids=message.hit_ids,
                        status=exc.status,
                        detail=str(exc),
                    )
                    async with self._db.session() as session:
                        await WarHitRepository(session).mark_posted(
                            message.hit_ids, REJECTED_MESSAGE_ID
                        )
                    continue
                self._send_ok("hits", war_row_id)
                async with self._db.session() as session:
                    await WarHitRepository(session).mark_posted(message.hit_ids, message_id)
                posted += len(message.hit_ids)

            if posted:
                logger.info("war.hits_published", war_row_id=war_row_id, count=posted)
            return posted

    async def publish_pending(self, war_row_id: int) -> int:
        async with self._events_lock:
            return await self._publish_pending(war_row_id)

    async def _publish_pending(self, war_row_id: int) -> int:
        async with self._db.session() as session:
            channel = await DiscordChannelRepository(session).get(war_row_id, "log")
            events = await WarLogRepository(session).unposted_for_war(war_row_id)

        if channel is None or not events:
            return 0

        posted = 0
        for event in events:
            message_id: int | None = None
            rejected = False
            try:
                for out in await self._messages_for(event):
                    message_id = await self._gateway.send_embeds(
                        channel.channel_id, out.embeds, attachments=out.attachments
                    )
            except GatewayNotReady:
                logger.info("war.log_publish_deferred", war_row_id=war_row_id)
                break
            except discord.HTTPException as exc:
                if not _rejected_message(exc):
                    self._send_blocked("log", war_row_id, exc)
                    break
                # A multi-message war summary may be partly posted; the rest is
                # dropped rather than re-sending the parts that did go out.
                logger.error(
                    "war.log_event_rejected",
                    war_row_id=war_row_id,
                    event_id=event.id,
                    kind=event.kind,
                    status=exc.status,
                    detail=str(exc),
                )
                rejected = True
            else:
                self._send_ok("log", war_row_id)
            async with self._db.session() as session:
                row = await session.get(WarLogEvent, event.id)
                if row is not None:
                    row.posted_message_id = REJECTED_MESSAGE_ID if rejected else message_id
            if not rejected:
                posted += 1

        if posted:
            logger.info("war.log_published", war_row_id=war_row_id, count=posted)
        return posted

    async def _messages_for(self, event: WarLogEvent) -> list[_Outgoing]:
        if event.kind != WAR_SUMMARY_KIND:
            return [_Outgoing([war_log_event_embed(event)])]

        async with self._db.session() as session:
            war = await WarRepository(session).get_by_id(event.war_id)
            hits = await WarHitRepository(session).war_hits(event.war_id)
            every_hit = await WarHitRepository(session).outgoing_hits(event.war_id)
            overdoses = await OverdoseRepository(session).since_watch(event.war_id)
        if war is None:
            return [_Outgoing([war_log_event_embed(event)])]
        players = summarise_hits(hits)
        messages = [
            _Outgoing(embeds)
            for embeds in war_summary_messages(
                our_faction_name=war.our_faction_name,
                enemy_faction_name=war.enemy_faction_name,
                result=war.result,
                players=players,
                overdoses=overdoses,
            )
        ]
        when = war.war_end or utcnow()
        files: list[tuple[str, bytes]] = []
        if players:
            files.append(
                (
                    summary_filename(war.our_faction_name, war.enemy_faction_name, when),
                    summary_csv(players),
                )
            )
        if every_hit:
            # Every hit (war + outside), in the payout sheet's raw layout, so admins
            # can paste it straight in and add the payouts.
            files.append(
                (
                    summary_filename(
                        war.our_faction_name, war.enemy_faction_name, when, prefix="war-hits"
                    ),
                    hits_csv(every_hit),
                )
            )
        if files:
            # The downloads go on the last message, so they sit under the table.
            messages[-1] = replace(messages[-1], attachments=tuple(files))
        return messages
