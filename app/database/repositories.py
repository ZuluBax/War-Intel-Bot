"""Repository layer — the only place that builds queries against the ORM models.

Services depend on these classes, not on raw ``select()`` statements, so query
logic stays in one place and is easy to unit-test against an in-memory database.

Phase 1 provides the write paths needed by war detection and the enemy roster.
Later phases extend this file; keep each method small and single-purpose.

``AsyncSession.scalar`` is typed as returning ``Any`` by SQLAlchemy, so every
read here binds the result to an explicitly typed local before returning it —
that keeps the public signatures honest under ``mypy --strict``.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ActivityWatch,
    ConfirmedStat,
    DiscordChannel,
    Faction,
    FlightRecord,
    Overdose,
    Player,
    PlayerActivity,
    PlayerStatusHistory,
    StatEstimate,
    TargetRanking,
    War,
    WarHit,
    WarLogEvent,
    WarPlayer,
)


class FactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, torn_faction_id: int, *, name: str | None, is_ours: bool) -> Faction:
        existing: Faction | None = await self._session.scalar(
            select(Faction).where(Faction.torn_faction_id == torn_faction_id)
        )
        if existing is None:
            existing = Faction(torn_faction_id=torn_faction_id, name=name, is_ours=is_ours)
            self._session.add(existing)
        else:
            if name is not None:
                existing.name = name
            existing.is_ours = existing.is_ours or is_ours
        await self._session.flush()
        return existing


class WarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_torn_war_id(self, guild_id: int, torn_war_id: int) -> War | None:
        row: War | None = await self._session.scalar(
            select(War).where(War.guild_id == guild_id, War.torn_war_id == torn_war_id)
        )
        return row

    async def get_by_id(self, war_id: int) -> War | None:
        return await self._session.get(War, war_id)

    async def get_active(self, guild_id: int) -> War | None:
        """Return this server's non-finished war, if one exists.

        Normally there is at most one. If an older war is still open (the bot missed
        its end; ``WarManager`` closes it on the next war check) the newest wins, so
        the boards follow the current war rather than an arbitrary one.
        """
        open_wars = await self.open_wars(guild_id)
        return open_wars[0] if open_wars else None

    async def open_wars(self, guild_id: int) -> list[War]:
        """This server's non-finished wars, newest first."""
        result = await self._session.scalars(
            select(War)
            .where(
                War.guild_id == guild_id,
                War.status.in_(("WAR_DETECTED", "WAR_ACTIVE")),
            )
            .order_by(War.id.desc())
        )
        return list(result)

    async def previous_workspaces(
        self, guild_id: int, *, exclude_war_id: int
    ) -> list[tuple[int, int]]:
        """``(war id, category id)`` of this server's other wars that had a per-war
        category, newest first. Wars run in single-channel mode have none, so they
        can never be mistaken for a reusable per-war layout."""
        result = await self._session.execute(
            select(War.id, War.discord_category_id)
            .where(
                War.guild_id == guild_id,
                War.id != exclude_war_id,
                War.discord_category_id.is_not(None),
            )
            .order_by(War.id.desc())
        )
        return [(war_id, category) for war_id, category in result.all() if category is not None]

    async def list_history(self, guild_id: int, *, limit: int = 10) -> list[War]:
        result = await self._session.scalars(
            select(War)
            .where(War.guild_id == guild_id, War.status == "WAR_FINISHED")
            .order_by(War.war_end.desc())
            .limit(limit)
        )
        return list(result)

    def add(self, war: War) -> War:
        self._session.add(war)
        return war


class PlayerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_torn_id(self, torn_player_id: int) -> Player | None:
        row: Player | None = await self._session.scalar(
            select(Player).where(Player.torn_player_id == torn_player_id)
        )
        return row

    async def upsert(
        self,
        torn_player_id: int,
        *,
        name: str | None = None,
        level: int | None = None,
        faction_id: int | None = None,
    ) -> Player:
        player = await self.get_by_torn_id(torn_player_id)
        if player is None:
            player = Player(
                torn_player_id=torn_player_id,
                name=name,
                level=level,
                last_seen_faction_id=faction_id,
            )
            self._session.add(player)
        else:
            if name is not None:
                player.name = name
            if level is not None:
                player.level = level
            if faction_id is not None:
                player.last_seen_faction_id = faction_id
        await self._session.flush()
        return player


class StatEstimateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, estimate: StatEstimate) -> StatEstimate:
        self._session.add(estimate)
        return estimate

    async def latest_for_player(self, player_id: int) -> StatEstimate | None:
        row: StatEstimate | None = await self._session.scalar(
            select(StatEstimate)
            .where(StatEstimate.player_id == player_id)
            .order_by(StatEstimate.observed_at.desc())
            .limit(1)
        )
        return row

    async def history_for_player(self, player_id: int, *, limit: int = 20) -> list[StatEstimate]:
        result = await self._session.scalars(
            select(StatEstimate)
            .where(StatEstimate.player_id == player_id)
            .order_by(StatEstimate.observed_at.desc())
            .limit(limit)
        )
        return list(result)


class FlightRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: FlightRecord) -> FlightRecord:
        self._session.add(row)
        return row

    async def airliner_methods(self, player_ids: Collection[int]) -> dict[int, str]:
        """Internal player id -> "BCT" / "Airline": the ticket on each player's most
        recent airliner flight we watched. Players with none are left out."""
        if not player_ids:
            return {}
        result = await self._session.execute(
            select(FlightRecord.player_id, FlightRecord.method)
            .where(
                FlightRecord.player_id.in_(list(player_ids)),
                FlightRecord.method.in_(("BCT", "Airline")),
            )
            .order_by(FlightRecord.landed_at)
        )
        return {pid: method for pid, method in result}  # later rows win: most recent


class ActivityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_player_slot(self, player_id: int, slot_start: datetime) -> None:
        """Idempotent: a slot already stored for this player is left alone."""
        exists = await self._session.scalar(
            select(PlayerActivity.id).where(
                PlayerActivity.player_id == player_id, PlayerActivity.slot_start == slot_start
            )
        )
        if exists is None:
            self._session.add(PlayerActivity(player_id=player_id, slot_start=slot_start))

    async def record_watch_slot(self, war_id: int, slot_start: datetime) -> None:
        exists = await self._session.scalar(
            select(ActivityWatch.id).where(
                ActivityWatch.war_id == war_id, ActivityWatch.slot_start == slot_start
            )
        )
        if exists is None:
            self._session.add(ActivityWatch(war_id=war_id, slot_start=slot_start))

    async def watch_slots(self, war_id: int, *, since: datetime) -> list[datetime]:
        result = await self._session.scalars(
            select(ActivityWatch.slot_start).where(
                ActivityWatch.war_id == war_id, ActivityWatch.slot_start >= since
            )
        )
        return list(result)

    async def player_slots(
        self, player_ids: Collection[int], *, since: datetime
    ) -> list[tuple[int, datetime]]:
        """``(internal player id, slot start)`` for every active slot since ``since``."""
        if not player_ids:
            return []
        result = await self._session.execute(
            select(PlayerActivity.player_id, PlayerActivity.slot_start).where(
                PlayerActivity.player_id.in_(list(player_ids)),
                PlayerActivity.slot_start >= since,
            )
        )
        return [(pid, slot) for pid, slot in result]


class StatusHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: PlayerStatusHistory) -> PlayerStatusHistory:
        self._session.add(row)
        return row

    async def latest_for_player(self, player_id: int) -> PlayerStatusHistory | None:
        row: PlayerStatusHistory | None = await self._session.scalar(
            select(PlayerStatusHistory)
            .where(PlayerStatusHistory.player_id == player_id)
            .order_by(PlayerStatusHistory.observed_at.desc())
            .limit(1)
        )
        return row

    async def seen_in_war(self, player_id: int, war_id: int) -> bool:
        """Whether any status snapshot of this player exists for this war yet."""
        row = await self._session.scalar(
            select(PlayerStatusHistory.id)
            .where(PlayerStatusHistory.player_id == player_id, PlayerStatusHistory.war_id == war_id)
            .limit(1)
        )
        return row is not None


@dataclass(frozen=True)
class OverdoseView:
    torn_player_id: int
    name: str | None
    drug: str
    first_seen_at: datetime
    hospital_until: datetime | None


class OverdoseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: Overdose) -> Overdose:
        self._session.add(row)
        return row

    async def latest(self, war_id: int, player_id: int) -> Overdose | None:
        row: Overdose | None = await self._session.scalar(
            select(Overdose)
            .where(Overdose.war_id == war_id, Overdose.player_id == player_id)
            .order_by(Overdose.first_seen_at.desc(), Overdose.id.desc())
            .limit(1)
        )
        return row

    async def since_watch(self, war_id: int) -> list[OverdoseView]:
        """This war's overdoses the bot saw happen (not ones from before it looked),
        newest first."""
        result = await self._session.execute(
            select(
                Player.torn_player_id,
                Player.name,
                Overdose.drug,
                Overdose.first_seen_at,
                Overdose.hospital_until,
            )
            .join(Player, Player.id == Overdose.player_id)
            .where(Overdose.war_id == war_id, Overdose.before_watch.is_(False))
            .order_by(Overdose.first_seen_at.desc(), Overdose.id.desc())
        )
        return [OverdoseView(*row) for row in result.all()]


class ConfirmedStatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, row: ConfirmedStat) -> ConfirmedStat:
        self._session.add(row)
        return row

    async def latest_for_player(self, guild_id: int, player_id: int) -> ConfirmedStat | None:
        """This server's newest report on a player. Never another server's."""
        row: ConfirmedStat | None = await self._session.scalar(
            select(ConfirmedStat)
            .where(ConfirmedStat.guild_id == guild_id, ConfirmedStat.player_id == player_id)
            .order_by(ConfirmedStat.reported_at.desc(), ConfirmedStat.id.desc())
            .limit(1)
        )
        return row

    async def latest_for_players(
        self, guild_id: int, player_ids: Collection[int]
    ) -> dict[int, ConfirmedStat]:
        """This server's newest report per player (internal ids), for a whole roster."""
        if not player_ids:
            return {}
        rows = await self._session.scalars(
            select(ConfirmedStat)
            .where(ConfirmedStat.guild_id == guild_id, ConfirmedStat.player_id.in_(player_ids))
            .order_by(ConfirmedStat.reported_at.desc(), ConfirmedStat.id.desc())
        )
        latest: dict[int, ConfirmedStat] = {}
        for row in rows:
            latest.setdefault(row.player_id, row)
        return latest


class WarPlayerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_war(self, war_id: int) -> list[WarPlayer]:
        result = await self._session.scalars(
            select(WarPlayer).where(WarPlayer.war_id == war_id)
        )
        return list(result)

    async def upsert(self, war_id: int, player_id: int, *, side: str = "ENEMY") -> WarPlayer:
        existing: WarPlayer | None = await self._session.scalar(
            select(WarPlayer).where(
                WarPlayer.war_id == war_id, WarPlayer.player_id == player_id
            )
        )
        if existing is None:
            existing = WarPlayer(war_id=war_id, player_id=player_id, side=side)
            self._session.add(existing)
            await self._session.flush()
        else:
            existing.left_faction = False  # present in the latest fetch -> not left
        return existing

    @staticmethod
    def observe_status(
        link: WarPlayer, description: str | None, *, now: datetime, max_gap: timedelta
    ) -> datetime | None:
        """Record what a player's status says right now and return when that exact
        status began — or ``None`` if we never saw it begin.

        The start is only trusted when the previous look was within ``max_gap``: a
        longer gap (bot down, first ever look) means the status may have started
        earlier, and a wrong take-off time would give a wrong landing time.
        """
        seen_recently = (
            link.status_checked_at is not None and now - link.status_checked_at <= max_gap
        )
        if link.status_description != description:
            link.status_description = description
            link.status_since = now if seen_recently else None
        elif not seen_recently:
            # Same words after a long gap could be a second, identical flight.
            link.status_since = None
        link.status_checked_at = now
        return link.status_since

    async def active_enemy_torn_ids(self, war_id: int) -> set[int]:
        """Torn player IDs currently linked as ENEMY and not marked left (spec §32)."""
        result = await self._session.execute(
            select(Player.torn_player_id)
            .join(WarPlayer, WarPlayer.player_id == Player.id)
            .where(
                WarPlayer.war_id == war_id,
                WarPlayer.side == "ENEMY",
                WarPlayer.left_faction.is_(False),
            )
        )
        return {row[0] for row in result}

    async def mark_left(self, war_id: int, torn_player_ids: set[int]) -> None:
        """Flag ENEMY links as having left the faction, keyed by Torn player ID."""
        if not torn_player_ids:
            return
        result = await self._session.execute(
            select(WarPlayer)
            .join(Player, WarPlayer.player_id == Player.id)
            .where(
                WarPlayer.war_id == war_id,
                WarPlayer.side == "ENEMY",
                Player.torn_player_id.in_(torn_player_ids),
            )
        )
        for wp in result.scalars():
            wp.left_faction = True
        await self._session.flush()


class DiscordChannelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def for_war(self, war_id: int) -> list[DiscordChannel]:
        result = await self._session.scalars(
            select(DiscordChannel).where(DiscordChannel.war_id == war_id)
        )
        return list(result)

    async def get(self, war_id: int, purpose: str) -> DiscordChannel | None:
        row: DiscordChannel | None = await self._session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == purpose
            )
        )
        return row

    def add(self, row: DiscordChannel) -> DiscordChannel:
        self._session.add(row)
        return row

    async def upsert_channel(
        self, war_id: int, purpose: str, channel_id: int
    ) -> DiscordChannel:
        existing = await self.get(war_id, purpose)
        if existing is None:
            existing = DiscordChannel(war_id=war_id, purpose=purpose, channel_id=channel_id)
            self._session.add(existing)
        elif existing.channel_id != channel_id:
            # The live message belongs to the old channel; keeping its id would make
            # every refresh try to edit a message that isn't in the new channel.
            existing.channel_id = channel_id
            existing.live_message_id = None
            existing.extra_message_ids = None
        await self._session.flush()
        return existing

    async def set_live_message(self, war_id: int, purpose: str, message_id: int) -> None:
        await self.set_live_messages(war_id, purpose, [message_id])

    async def set_live_messages(self, war_id: int, purpose: str, message_ids: list[int]) -> None:
        """A board's messages in order: the first is ``live_message_id``, any after it
        (a board split over several messages) go in ``extra_message_ids``."""
        row = await self.get(war_id, purpose)
        if row is not None:
            row.live_message_id = message_ids[0] if message_ids else None
            row.extra_message_ids = ",".join(str(m) for m in message_ids[1:]) or None
            await self._session.flush()


def message_ids_of(row: DiscordChannel) -> list[int]:
    """The board's live message ids, in order (``live_message_id`` + extras)."""
    extras = [int(m) for m in (row.extra_message_ids or "").split(",") if m.strip()]
    first = [row.live_message_id] if row.live_message_id is not None else []
    return first + extras


class WarLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, event: WarLogEvent) -> WarLogEvent:
        self._session.add(event)
        return event

    async def unposted_for_war(self, war_id: int) -> list[WarLogEvent]:
        result = await self._session.scalars(
            select(WarLogEvent)
            .where(WarLogEvent.war_id == war_id, WarLogEvent.posted_message_id.is_(None))
            # id breaks ties: events written in one transaction share a timestamp,
            # and "war ended" must post before the scoreboard that follows it.
            .order_by(WarLogEvent.occurred_at.asc(), WarLogEvent.id.asc())
        )
        return list(result)


@dataclass(frozen=True)
class StoredHits:
    new: int  # rows inserted
    filled: int  # already-stored rows that just gained their chain number / warlord bonus


class WarHitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def earliest_incomplete(self, war_id: int) -> datetime | None:
        """Start time of the oldest stored hit still missing its chain number or its
        warlord bonus (i.e. stored before those columns existed)."""
        earliest: datetime | None = await self._session.scalar(
            select(func.min(WarHit.started_at)).where(
                WarHit.war_id == war_id,
                or_(WarHit.chain.is_(None), WarHit.warlord.is_(None)),
            )
        )
        return earliest

    async def latest_started(self, war_id: int) -> datetime | None:
        latest: datetime | None = await self._session.scalar(
            select(func.max(WarHit.started_at)).where(WarHit.war_id == war_id)
        )
        return latest

    async def add_new(self, war_id: int, hits: list[WarHit]) -> StoredHits:
        """Insert the hits not already stored for this war.

        The sync deliberately re-reads a short overlap of the feed each tick (a
        hit can appear in Torn's API a little after a later one), so most of
        what arrives is already here. A stored hit that predates the chain / warlord
        columns (NULL) is completed from the fresh copy; nothing else about a stored
        row is ever overwritten.
        """
        if not hits:
            return StoredHits(0, 0)
        stored = {
            row.torn_attack_id: row
            for row in await self._session.scalars(
                select(WarHit).where(
                    WarHit.war_id == war_id,
                    WarHit.torn_attack_id.in_([h.torn_attack_id for h in hits]),
                )
            )
        }
        fresh = [h for h in hits if h.torn_attack_id not in stored]
        filled = 0
        for hit in hits:
            row = stored.get(hit.torn_attack_id)
            if row is not None and (row.chain is None or row.warlord is None):
                row.chain = hit.chain if row.chain is None else row.chain
                row.warlord = hit.warlord if row.warlord is None else row.warlord
                filled += 1
        for hit in fresh:
            hit.war_id = war_id
            self._session.add(hit)
        await self._session.flush()
        return StoredHits(len(fresh), filled)

    async def war_hits(self, war_id: int) -> list[WarHit]:
        """Every hit our faction made in the war (for the end-of-war scoreboard)."""
        result = await self._session.scalars(
            select(WarHit)
            .where(
                WarHit.war_id == war_id,
                WarHit.is_outgoing.is_(True),
                WarHit.is_war_hit.is_(True),
            )
            .order_by(WarHit.started_at.asc(), WarHit.id.asc())
        )
        return list(result)

    async def outgoing_hits(self, war_id: int) -> list[WarHit]:
        """Every hit our faction made in the war window, war and outside alike."""
        result = await self._session.scalars(
            select(WarHit)
            .where(WarHit.war_id == war_id, WarHit.is_outgoing.is_(True))
            .order_by(WarHit.started_at.asc(), WarHit.id.asc())
        )
        return list(result)

    async def unposted_outgoing(self, war_id: int, *, limit: int) -> list[WarHit]:
        result = await self._session.scalars(
            select(WarHit)
            .where(
                WarHit.war_id == war_id,
                WarHit.is_outgoing.is_(True),
                WarHit.posted_message_id.is_(None),
            )
            .order_by(WarHit.started_at.asc(), WarHit.id.asc())
            .limit(limit)
        )
        return list(result)

    async def mark_posted(self, hit_ids: list[int], message_id: int) -> None:
        await self._session.execute(
            update(WarHit).where(WarHit.id.in_(hit_ids)).values(posted_message_id=message_id)
        )


class TargetRankingRepository:
    """``target_rankings`` is a **snapshot**, not history (spec §17) — each
    refresh replaces the whole set for a war rather than appending to it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_war(self, war_id: int, rankings: list[TargetRanking]) -> None:
        await self._session.execute(delete(TargetRanking).where(TargetRanking.war_id == war_id))
        for row in rankings:
            self._session.add(row)
        await self._session.flush()

    async def for_war(self, war_id: int) -> list[TargetRanking]:
        result = await self._session.scalars(
            select(TargetRanking)
            .where(TargetRanking.war_id == war_id)
            .order_by(TargetRanking.rank)
        )
        return list(result)
