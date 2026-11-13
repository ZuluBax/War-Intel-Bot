from __future__ import annotations

import time
from typing import Any, cast

import discord
import pytest
from sqlalchemy import func, select

from app.database.database import Database
from app.database.models import DiscordChannel, Faction, War, WarLogEvent
from app.database.repositories import DiscordChannelRepository, WarLogRepository
from app.discord import war_channels
from app.discord.abroad_picker import ABROAD_PICKER_ID
from app.discord.embeds import base_embed
from app.discord.gateway import GatewayNotReady, GuildGateway
from app.discord.war_channels import CHANNEL_LAYOUT, WarBoard, WarChannelManager, WarLogBoard
from app.discord.war_publish import DiscordWarPublisher
from app.torn.models import PlayerState
from app.war.roster import Roster, RosterEntry
from app.war.target_service import TargetView
from app.war.warlog import roster_join_event, state_change_event
from tests.fake_gateway import FakeGuildGateway, discord_http_error


async def _seed_war(db: Database, *, enemy_name: str = "Rival Crew") -> int:
    async with db.session() as session:
        us = Faction(torn_faction_id=53228, name="Null & Void", is_ours=True)
        them = Faction(torn_faction_id=60001, name=enemy_name, is_ours=False)
        session.add_all([us, them])
        await session.flush()
        war = War(
            guild_id=99001,
            torn_war_id=77001,
            our_faction_id=us.id,
            our_faction_name="Null & Void",
            enemy_faction_id=them.id,
            enemy_faction_name=enemy_name,
            status="WAR_ACTIVE",
        )
        session.add(war)
        await session.flush()
        return war.id


def _gw(fake: FakeGuildGateway) -> GuildGateway:
    return cast(GuildGateway, fake)


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int((await session.scalar(select(func.count()).select_from(model))) or 0)


async def test_ensure_workspace_creates_once_and_is_idempotent(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))

    ws1 = await mgr.ensure_workspace(war_id)
    assert len(fake.categories) == 1
    assert len(fake.channels) == len(CHANNEL_LAYOUT) == 5
    assert set(ws1.channel_ids) == {p for p, _, _ in CHANNEL_LAYOUT}
    assert await _count(database, DiscordChannel) == len(CHANNEL_LAYOUT)

    ws2 = await mgr.ensure_workspace(war_id)
    assert ws2 == ws1  # same ids
    assert len(fake.categories) == 1
    assert len(fake.channels) == len(CHANNEL_LAYOUT)
    assert await _count(database, DiscordChannel) == len(CHANNEL_LAYOUT)


async def test_ensure_workspace_recreates_a_deleted_channel(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))

    ws1 = await mgr.ensure_workspace(war_id)
    stats_channel = ws1.channel_ids["stats"]
    fake.delete_channel(stats_channel)

    ws2 = await mgr.ensure_workspace(war_id)
    assert ws2.channel_ids["stats"] != stats_channel  # recreated
    assert ws2.channel_ids["information"] == ws1.channel_ids["information"]  # untouched
    assert len(fake.channels) == len(CHANNEL_LAYOUT)
    assert await _count(database, DiscordChannel) == len(CHANNEL_LAYOUT)


async def test_single_channel_mode_maps_every_purpose_to_one_channel(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    war_room_id = 555000111
    fake.channels[war_room_id] = {"name": "war-room", "category_id": None, "topic": ""}
    mgr = WarChannelManager(database, _gw(fake), single_channel_id=war_room_id)

    ws = await mgr.ensure_workspace(war_id)

    assert set(ws.channel_ids.values()) == {war_room_id}
    assert len(fake.categories) == 0  # no category created in single-channel mode
    assert len(fake.channels) == 1  # no new channels created either
    assert await _count(database, DiscordChannel) == len(CHANNEL_LAYOUT)


async def test_single_channel_mode_lets_the_board_post_and_edit_in_it(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    war_room_id = 555000222
    fake.channels[war_room_id] = {"name": "war-room", "category_id": None, "topic": ""}
    await WarChannelManager(database, _gw(fake), single_channel_id=war_room_id).ensure_workspace(
        war_id
    )
    board = WarBoard(database, _gw(fake))

    await board.publish(war_id, "information", base_embed("v1"))
    await board.publish(war_id, "stats", base_embed("v1"))
    await board.publish(war_id, "information", base_embed("v2"))  # edits, doesn't repost

    assert fake.message_count(war_room_id) == 2  # one message per purpose, both in this channel


async def test_single_channel_mode_raises_gateway_not_ready_if_channel_is_gone(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake), single_channel_id=999999999)  # never registered
    with pytest.raises(GatewayNotReady):
        await mgr.ensure_workspace(war_id)


async def test_warboard_posts_then_edits_same_message(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))

    await board.publish(war_id, "information", base_embed("v1"))
    await board.publish(war_id, "information", base_embed("v2"))

    async with database.session() as session:
        row = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "information"
            )
        )
    assert row is not None and row.live_message_id is not None
    assert fake.message_count(row.channel_id) == 1  # edited, not reposted


async def test_warboard_skips_identical_edit(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))
    edits = 0
    real_edit = fake.edit_embeds

    async def counting_edit(*args: Any, **kwargs: Any) -> bool:
        nonlocal edits
        edits += 1
        return await real_edit(*args, **kwargs)

    fake.edit_embeds = counting_edit  # type: ignore[method-assign]

    assert await board.publish(war_id, "information", base_embed("v1"))  # first post
    assert await board.publish(war_id, "information", base_embed("v1"))  # unchanged: no call
    assert edits == 0
    assert await board.publish(war_id, "information", base_embed("v2"))  # changed: edits
    assert edits == 1


async def test_warboard_rechecks_unchanged_board_after_a_while(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board deleted by hand is reposted even if its content never changes."""
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))
    await board.publish(war_id, "stats", base_embed("same"))
    async with database.session() as session:
        row = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "stats"
            )
        )
    assert row is not None and row.live_message_id is not None
    fake.delete_message(row.channel_id, row.live_message_id)

    later = time.monotonic() + 301
    monkeypatch.setattr(war_channels.time, "monotonic", lambda: later)
    await board.publish(war_id, "stats", base_embed("same"))
    assert fake.message_count(row.channel_id) == 1  # reposted


async def test_warboard_reposts_if_message_was_deleted(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))

    await board.publish(war_id, "stats", base_embed("v1"))
    async with database.session() as session:
        row = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "stats"
            )
        )
    assert row is not None
    first_message_id = row.live_message_id
    assert first_message_id is not None
    fake.delete_message(row.channel_id, first_message_id)

    await board.publish(war_id, "stats", base_embed("v2"))
    async with database.session() as session:
        row2 = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "stats"
            )
        )
    assert row2 is not None and row2.live_message_id != first_message_id


async def test_warboard_accepts_a_list_of_embeds_as_one_message(database: Database) -> None:
    """A paginated embed (e.g. a large roster) is multiple embeds in ONE
    message, not multiple messages — this is what roster_embed()/
    target_board_embed() return once they outgrow a single embed."""
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))

    await board.publish(war_id, "stats", [base_embed("page 1"), base_embed("page 2")])

    async with database.session() as session:
        row = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "stats"
            )
        )
    assert row is not None and row.live_message_id is not None
    assert fake.message_count(row.channel_id) == 1  # one message...
    assert len(fake.messages[(row.channel_id, row.live_message_id)]) == 2  # ...holding both embeds

    # A refresh back down to one embed replaces the message's embeds, not just edits page 1.
    await board.publish(war_id, "stats", base_embed("page 1 only"))
    assert fake.message_count(row.channel_id) == 1
    assert len(fake.messages[(row.channel_id, row.live_message_id)]) == 1


async def test_warboard_rejects_unknown_purpose(database: Database) -> None:
    war_id = await _seed_war(database)
    board = WarBoard(database, _gw(FakeGuildGateway()))
    with pytest.raises(ValueError, match="purpose"):
        await board.publish(war_id, "nonsense", base_embed("x"))


def _roster(war_id: int) -> Roster:
    return Roster(
        war_id=war_id,
        enemy_faction_id=60001,
        enemy_faction_name="Rival Crew",
        entries=[
            RosterEntry(player_id=1, name="A", level=40, estimated_bs=2.0e9, fair_fight=1.5),
            RosterEntry(player_id=2, name="B", level=30, estimated_bs=1.0e9, fair_fight=2.1),
        ],
    )


async def test_publisher_builds_workspace_and_posts_two_live_embeds(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = DiscordWarPublisher(
        database,
        WarChannelManager(database, _gw(fake)),
        WarBoard(database, _gw(fake)),
        WarLogBoard(database, _gw(fake)),
    )

    await publisher.publish_war(war_id, _roster(war_id))
    await publisher.publish_war(war_id, _roster(war_id))  # refresh

    assert len(fake.categories) == 1
    assert len(fake.channels) == len(CHANNEL_LAYOUT)
    async with database.session() as session:
        info = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "information"
            )
        )
        stats = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "stats"
            )
        )
    assert info is not None and fake.message_count(info.channel_id) == 1
    assert stats is not None and fake.message_count(stats.channel_id) == 1


async def test_publisher_defers_when_gateway_not_ready(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway(ready=False)
    publisher = DiscordWarPublisher(
        database,
        WarChannelManager(database, _gw(fake)),
        WarBoard(database, _gw(fake)),
        WarLogBoard(database, _gw(fake)),
    )
    # Must not raise.
    await publisher.publish_war(war_id, _roster(war_id))
    assert len(fake.categories) == 0


# --- WarLogBoard (Phase 5, spec §32) -----------------------------------------


async def test_log_board_posts_each_pending_event_once_and_marks_posted(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)

    async with database.session() as session:
        logs = WarLogRepository(session)
        logs.add(state_change_event(war_id, was="WAR_DETECTED", now="WAR_ACTIVE"))
        logs.add(roster_join_event(war_id, player_id=1, name="NewEnemy"))

    board = WarLogBoard(database, _gw(fake))
    posted = await board.publish_pending(war_id)
    assert posted == 2

    async with database.session() as session:
        log_channel = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "log"
            )
        )
        events = list(
            await session.scalars(select(WarLogEvent).where(WarLogEvent.war_id == war_id))
        )
    assert log_channel is not None
    assert fake.message_count(log_channel.channel_id) == 2
    assert all(e.posted_message_id is not None for e in events)

    # A second call finds nothing left to post.
    assert await board.publish_pending(war_id) == 0


async def _three_pending_events(db: Database) -> tuple[int, int, WarLogBoard, FakeGuildGateway]:
    war_id = await _seed_war(db)
    fake = FakeGuildGateway()
    await WarChannelManager(db, _gw(fake)).ensure_workspace(war_id)
    async with db.session() as session:
        logs = WarLogRepository(session)
        for pid in (1, 2, 3):
            logs.add(roster_join_event(war_id, player_id=pid, name=f"Enemy{pid}"))
        log_channel = await DiscordChannelRepository(session).get(war_id, "log")
    assert log_channel is not None
    return war_id, log_channel.channel_id, WarLogBoard(db, _gw(fake)), fake


async def _unposted(db: Database, war_id: int) -> list[str]:
    async with db.session() as session:
        return [e.message for e in await WarLogRepository(session).unposted_for_war(war_id)]


async def test_log_board_skips_an_event_discord_rejects_and_posts_the_rest(
    database: Database,
) -> None:
    # A 400 is about that one message (e.g. too large) — retrying it can never work,
    # so it must not hold up every event queued behind it.
    war_id, channel_id, board, fake = await _three_pending_events(database)
    fake.send_failures = [None, discord_http_error(discord.HTTPException, 400)]

    assert await board.publish_pending(war_id) == 2
    assert fake.message_count(channel_id) == 2
    assert await _unposted(database, war_id) == []  # the bad one is marked, not retried

    assert await board.publish_pending(war_id) == 0
    assert fake.message_count(channel_id) == 2


@pytest.mark.parametrize(
    ("error_cls", "status"),
    [
        (discord.Forbidden, 403),  # bot lost its permission in the channel
        (discord.NotFound, 404),  # channel deleted under us
        (discord.DiscordServerError, 503),  # Discord outage
    ],
)
async def test_log_board_keeps_events_queued_when_the_channel_or_discord_fails(
    database: Database, error_cls: type[discord.HTTPException], status: int
) -> None:
    # Not the message's fault: post nothing, drop nothing, and retry next pass.
    war_id, channel_id, board, fake = await _three_pending_events(database)
    fake.send_failures = [discord_http_error(error_cls, status)]

    assert await board.publish_pending(war_id) == 0
    assert fake.message_count(channel_id) == 0
    assert len(await _unposted(database, war_id)) == 3

    # Once the problem clears, everything goes out, in order.
    assert await board.publish_pending(war_id) == 3
    assert fake.message_count(channel_id) == 3
    assert await _unposted(database, war_id) == []


async def test_log_board_is_a_noop_without_a_log_channel(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    async with database.session() as session:
        WarLogRepository(session).add(
            state_change_event(war_id, was="WAR_DETECTED", now="WAR_ACTIVE")
        )
    # No ensure_workspace() call -> no "log" channel row yet.
    board = WarLogBoard(database, _gw(fake))
    assert await board.publish_pending(war_id) == 0


# --- DiscordWarPublisher.publish_targets (Phase 6) ---------------------------


def _targets() -> list[TargetView]:
    return [
        TargetView(
            player_id=1,
            name="Weak",
            level=20,
            rank=1,
            score=0.9,
            priority="HIGH",
            state=PlayerState.OKAY,
            is_available=True,
        ),
        TargetView(
            player_id=2,
            name="Tough",
            level=50,
            rank=2,
            score=0.1,
            priority="LOW",
            state=PlayerState.HOSPITAL,
            is_available=False,
        ),
    ]


async def test_publisher_publishes_targets_to_the_targets_channel(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = DiscordWarPublisher(
        database,
        WarChannelManager(database, _gw(fake)),
        WarBoard(database, _gw(fake)),
        WarLogBoard(database, _gw(fake)),
    )

    await publisher.publish_war(war_id, _roster(war_id))  # creates the workspace
    await publisher.publish_targets(war_id, _targets())

    async with database.session() as session:
        targets_channel = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "targets"
            )
        )
    assert targets_channel is not None
    assert fake.message_count(targets_channel.channel_id) == 1


async def test_clear_targets_edits_the_same_message_to_a_war_over_notice(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = DiscordWarPublisher(
        database,
        WarChannelManager(database, _gw(fake)),
        WarBoard(database, _gw(fake)),
        WarLogBoard(database, _gw(fake)),
    )

    await publisher.publish_war(war_id, _roster(war_id))  # creates the workspace
    await publisher.publish_targets(war_id, _targets())
    await publisher.clear_targets(war_id)

    async with database.session() as session:
        targets_channel = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "targets"
            )
        )
    assert targets_channel is not None
    # Edited in place, not a second message — the stale list simply becomes the notice.
    assert fake.message_count(targets_channel.channel_id) == 1
    [(_, embeds)] = [
        (mid, embeds)
        for (cid, mid), embeds in fake.messages.items()
        if cid == targets_channel.channel_id
    ]
    assert "war over" in (embeds[0].description or "").lower()
    assert "Weak" not in (embeds[0].description or "")  # the old target list is gone


# --- reusing one category + channels across wars ------------------------------
# The owner locks these channels (bot-only) by hand — the bot cannot set channel
# permissions itself — so a new war must land in the same, already-locked ones.


async def _add_war(
    db: Database,
    *,
    guild_id: int = 99001,
    torn_war_id: int = 77002,
    enemy_name: str = "Second Foe",
    enemy_torn_id: int = 60002,
) -> int:
    async with db.session() as session:
        us = await session.scalar(select(Faction).where(Faction.torn_faction_id == 53228))
        assert us is not None
        them = Faction(torn_faction_id=enemy_torn_id, name=enemy_name, is_ours=False)
        session.add(them)
        await session.flush()
        war = War(
            guild_id=guild_id,
            torn_war_id=torn_war_id,
            our_faction_id=us.id,
            our_faction_name="Null & Void",
            enemy_faction_id=them.id,
            enemy_faction_name=enemy_name,
            status="WAR_ACTIVE",
        )
        session.add(war)
        await session.flush()
        return war.id


async def test_a_new_war_reuses_the_earlier_categorys_locked_channels(database: Database) -> None:
    first = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))
    ws1 = await mgr.ensure_workspace(first)

    second = await _add_war(database)
    ws2 = await mgr.ensure_workspace(second)

    assert ws2 == ws1  # same category, same four channels
    assert len(fake.categories) == 1
    assert len(fake.channels) == len(CHANNEL_LAYOUT)  # nothing new created
    assert await _count(database, DiscordChannel) == 2 * len(CHANNEL_LAYOUT)  # a row set per war
    assert "Second Foe" in fake.categories[ws2.category_id]  # renamed for the current war


async def test_reuse_is_idempotent_across_repeated_calls(database: Database) -> None:
    first = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))
    await mgr.ensure_workspace(first)
    second = await _add_war(database)

    ws_a = await mgr.ensure_workspace(second)
    ws_b = await mgr.ensure_workspace(second)

    assert ws_a == ws_b
    assert len(fake.channels) == len(CHANNEL_LAYOUT) and len(fake.categories) == 1
    assert await _count(database, DiscordChannel) == 2 * len(CHANNEL_LAYOUT)


async def test_a_deleted_earlier_category_means_a_fresh_one(database: Database) -> None:
    first = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))
    ws1 = await mgr.ensure_workspace(first)
    fake.categories.pop(ws1.category_id)
    for cid in ws1.channel_ids.values():
        fake.delete_channel(cid)

    ws2 = await mgr.ensure_workspace(await _add_war(database))

    assert ws2.category_id != ws1.category_id
    assert set(ws2.channel_ids.values()).isdisjoint(ws1.channel_ids.values())
    assert len(fake.categories) == 1 and len(fake.channels) == len(CHANNEL_LAYOUT)


async def test_only_the_missing_channel_is_recreated_when_reusing(database: Database) -> None:
    first = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))
    ws1 = await mgr.ensure_workspace(first)
    fake.delete_channel(ws1.channel_ids["targets"])

    ws2 = await mgr.ensure_workspace(await _add_war(database))

    assert ws2.category_id == ws1.category_id
    assert ws2.channel_ids["targets"] != ws1.channel_ids["targets"]  # recreated
    for purpose in ("information", "stats", "log"):
        assert ws2.channel_ids[purpose] == ws1.channel_ids[purpose]  # reused
    assert fake.channels[ws2.channel_ids["targets"]]["category_id"] == ws1.category_id
    assert len(fake.channels) == len(CHANNEL_LAYOUT)


async def test_another_servers_workspace_is_never_reused(database: Database) -> None:
    first = await _seed_war(database)  # guild 99001
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))
    ws1 = await mgr.ensure_workspace(first)

    other_guild = await _add_war(database, guild_id=42, torn_war_id=88001)
    ws2 = await mgr.ensure_workspace(other_guild)

    assert ws2.category_id != ws1.category_id
    assert len(fake.categories) == 2 and len(fake.channels) == 2 * len(CHANNEL_LAYOUT)


async def test_a_single_channel_server_is_not_mistaken_for_a_per_war_layout(
    database: Database,
) -> None:
    """A server that used ``/setup channel`` has no category; if it later drops that
    setting, its one shared channel must not be adopted as all four purposes."""
    first = await _seed_war(database)
    fake = FakeGuildGateway()
    war_room = 555000333
    fake.channels[war_room] = {"name": "war-room", "category_id": None, "topic": ""}
    await WarChannelManager(database, _gw(fake), single_channel_id=war_room).ensure_workspace(first)

    ws = await WarChannelManager(database, _gw(fake)).ensure_workspace(await _add_war(database))

    assert war_room not in ws.channel_ids.values()
    assert len(fake.categories) == 1  # a fresh per-war category was created
    assert len(ws.channel_ids) == len(CHANNEL_LAYOUT) and len(set(ws.channel_ids.values())) == len(
        CHANNEL_LAYOUT
    )


async def test_the_new_war_posts_fresh_boards_and_leaves_the_old_ones_alone(
    database: Database,
) -> None:
    first = await _seed_war(database)
    fake = FakeGuildGateway()
    mgr = WarChannelManager(database, _gw(fake))
    board = WarBoard(database, _gw(fake))
    ws = await mgr.ensure_workspace(first)
    await board.publish(first, "information", base_embed("first war"))

    second = await _add_war(database)
    await mgr.ensure_workspace(second)
    await board.publish(second, "information", base_embed("second war"))

    info_channel = ws.channel_ids["information"]
    # the finished war's snapshot is kept; the new war gets its own message below it
    assert fake.message_count(info_channel) == 2


def _abroad_view(player_id: int, country: str) -> TargetView:
    return TargetView(
        player_id=player_id,
        name=f"Away{player_id}",
        level=30,
        rank=player_id,
        score=0.5,
        priority="MEDIUM",
        state=PlayerState.TRAVELING,
        is_available=False,
        status_description=f"In {country}",
    )


def _country_picker(fake: FakeGuildGateway, channel_id: int) -> discord.ui.Select | None:
    [view] = [v for (cid, _), v in fake.views.items() if cid == channel_id]
    selects = [item for item in view.children if isinstance(item, discord.ui.Select)]
    return selects[0] if selects else None


async def test_target_board_carries_a_country_picker_that_follows_who_is_abroad(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = DiscordWarPublisher(
        database,
        WarChannelManager(database, _gw(fake)),
        WarBoard(database, _gw(fake)),
        WarLogBoard(database, _gw(fake)),
    )
    await publisher.publish_war(war_id, _roster(war_id))
    targets = [*_targets(), _abroad_view(3, "Japan"), _abroad_view(4, "Argentina")]
    await publisher.publish_targets(war_id, targets)

    async with database.session() as session:
        row = await DiscordChannelRepository(session).get(war_id, "targets")
    assert row is not None
    picker = _country_picker(fake, row.channel_id)
    assert picker is not None
    assert picker.custom_id == ABROAD_PICKER_ID
    assert [o.value for o in picker.options] == ["Argentina", "Japan"]  # A to Z

    # Nobody abroad any more -> the refresh takes the drop-down away.
    await publisher.publish_targets(war_id, _targets())
    assert _country_picker(fake, row.channel_id) is None

    # War over -> no drop-down on the cleared board either.
    await publisher.publish_targets(war_id, targets)
    await publisher.clear_targets(war_id)
    assert _country_picker(fake, row.channel_id) is None


async def test_moving_a_board_to_another_channel_forgets_the_old_live_message(
    database: Database,
) -> None:
    # /setup channel after a war started remaps every purpose to the war-room channel;
    # the old message id points into the old channel and must not be edited there.
    war_id = await _seed_war(database)
    async with database.session() as session:
        repo = DiscordChannelRepository(session)
        await repo.upsert_channel(war_id, "targets", 111)
        await repo.set_live_message(war_id, "targets", 555)
        await repo.upsert_channel(war_id, "targets", 111)  # same channel: keep it
        row = await repo.get(war_id, "targets")
        assert row is not None and row.live_message_id == 555
        await repo.upsert_channel(war_id, "targets", 222)
        row = await repo.get(war_id, "targets")
        assert row is not None
        assert (row.channel_id, row.live_message_id) == (222, None)


async def test_clearing_setup_channel_mid_war_moves_boards_into_five_new_channels(
    database: Database,
) -> None:
    # Null & Void, 29 Sep 2026: /setup channel was set after the war started, then
    # cleared. Every purpose row still pointed at the one war-room channel, which
    # exists, so per-war mode kept reusing it and never made the 5 channels.
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    war_room = 555000333
    fake.channels[war_room] = {"name": "war-utilities", "category_id": None, "topic": ""}
    await WarChannelManager(database, _gw(fake), single_channel_id=war_room).ensure_workspace(
        war_id
    )

    ws = await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)

    assert war_room not in ws.channel_ids.values()
    assert len(set(ws.channel_ids.values())) == len(CHANNEL_LAYOUT)
    assert len(fake.categories) == 1


# --- own channels (/setup board): the bot never creates anything ----------------


def _own_channels(fake: FakeGuildGateway, *purposes: str) -> dict[str, int]:
    boards: dict[str, int] = {}
    for i, purpose in enumerate(purposes):
        channel_id = 700000 + i
        fake.channels[channel_id] = {"name": purpose, "category_id": None, "topic": ""}
        boards[purpose] = channel_id
    return boards


async def test_own_channels_mode_posts_each_board_in_its_channel(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    boards = _own_channels(fake, *(p for p, _, _ in CHANNEL_LAYOUT))
    channels_before = set(fake.channels)

    mgr = WarChannelManager(database, _gw(fake), board_channels=boards)
    workspace = await mgr.ensure_workspace(war_id)

    assert workspace.channel_ids == boards
    assert set(fake.channels) == channels_before  # nothing created
    assert fake.categories == {}
    board = WarBoard(database, _gw(fake))
    for purpose, channel_id in boards.items():
        assert await board.publish(war_id, purpose, base_embed(purpose))
        assert fake.message_count(channel_id) == 1


async def test_own_channels_mode_falls_back_to_the_setup_channel_room(
    database: Database,
) -> None:
    """Mid-switch: boards not moved yet keep posting in the old shared room."""
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    boards = _own_channels(fake, "information")
    room = 555000333
    fake.channels[room] = {"name": "war-utilities", "category_id": None, "topic": ""}

    mgr = WarChannelManager(database, _gw(fake), single_channel_id=room, board_channels=boards)
    workspace = await mgr.ensure_workspace(war_id)

    assert workspace.channel_ids["information"] == boards["information"]
    assert all(workspace.channel_ids[p] == room for p in ("stats", "targets", "log", "activity"))


async def test_own_channels_mode_skips_unset_and_missing_channels(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    boards = _own_channels(fake, "information", "stats")
    fake.delete_channel(boards["stats"])

    workspace = await WarChannelManager(
        database, _gw(fake), board_channels=boards
    ).ensure_workspace(war_id)

    assert workspace.channel_ids == {"information": boards["information"]}
    assert fake.categories == {}
    assert set(fake.channels) == {boards["information"]}  # still nothing created


async def test_switching_from_one_room_to_own_channels_moves_the_boards(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    room = 555000444
    fake.channels[room] = {"name": "war-utilities", "category_id": None, "topic": ""}
    board = WarBoard(database, _gw(fake))
    await WarChannelManager(database, _gw(fake), single_channel_id=room).ensure_workspace(war_id)
    await board.publish(war_id, "targets", base_embed("old"))

    boards = _own_channels(fake, "targets")
    await WarChannelManager(
        database, _gw(fake), single_channel_id=room, board_channels=boards
    ).ensure_workspace(war_id)
    await board.publish(war_id, "targets", base_embed("new"))

    assert fake.message_count(boards["targets"]) == 1  # posted fresh in the new channel


# --- no Manage Channels in per-war mode: don't retry a forbidden create every tick ---


async def test_without_manage_channels_boards_stay_in_the_old_room_and_nothing_is_tried(
    database: Database,
) -> None:
    # Null & Void, 1 Oct 2026: /setup channel cleared with Manage Channels already
    # removed. Every 15s tick tried to create channels and got 403 (50013).
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    war_room = 555000333
    fake.channels[war_room] = {"name": "war-utilities", "category_id": None, "topic": ""}
    await WarChannelManager(database, _gw(fake), single_channel_id=war_room).ensure_workspace(
        war_id
    )
    fake.manage_channels = False

    mgr = WarChannelManager(database, _gw(fake))
    for _ in range(3):
        ws = await mgr.ensure_workspace(war_id)

    assert fake.create_attempts == 0
    assert ws.channel_ids == {p: war_room for p, _, _ in CHANNEL_LAYOUT}


async def test_without_manage_channels_and_no_channels_nothing_is_posted_or_tried(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway(manage_channels=False)

    ws = await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)

    assert fake.create_attempts == 0
    assert ws.channel_ids == {}
    assert await _count(database, DiscordChannel) == 0


async def test_a_refused_create_backs_off_then_retries(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    fake.refuse_creates = True  # permission looks held, Discord says no anyway
    clock = [1000.0]
    monkeypatch.setattr(war_channels.time, "monotonic", lambda: clock[0])
    mgr = WarChannelManager(database, _gw(fake))

    await mgr.ensure_workspace(war_id)
    assert fake.create_attempts == 1  # stops at the first refusal, not one per channel

    clock[0] += 60
    await mgr.ensure_workspace(war_id)
    assert fake.create_attempts == 1  # still backing off

    fake.refuse_creates = False
    clock[0] += war_channels._CREATE_RETRY_SECONDS
    ws = await mgr.ensure_workspace(war_id)
    assert len(fake.categories) == 1
    assert len(set(ws.channel_ids.values())) == len(CHANNEL_LAYOUT)


async def test_channels_are_made_once_manage_channels_is_given_back(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway(manage_channels=False)
    mgr = WarChannelManager(database, _gw(fake))
    await mgr.ensure_workspace(war_id)

    fake.manage_channels = True
    ws = await mgr.ensure_workspace(war_id)

    assert len(set(ws.channel_ids.values())) == len(CHANNEL_LAYOUT)


# --- a board split over two messages (target board with a big roster) --------------


async def _targets_row(db: Database, war_id: int) -> DiscordChannel:
    async with db.session() as session:
        row = await DiscordChannelRepository(session).get(war_id, "targets")
    assert row is not None
    return row


def _live(fake: FakeGuildGateway, channel_id: int) -> list[int]:
    return sorted(
        mid for (cid, mid) in fake.messages if cid == channel_id and (cid, mid) not in fake.deleted
    )


async def test_board_in_two_messages_posts_both_then_edits_both_in_place(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))

    assert await board.publish_messages(
        war_id, "targets", [[base_embed("part 1")], [base_embed("part 2")]]
    )
    row = await _targets_row(database, war_id)
    first = _live(fake, row.channel_id)
    assert len(first) == 2
    assert row.live_message_id == first[0]
    assert row.extra_message_ids == str(first[1])

    await board.publish_messages(
        war_id, "targets", [[base_embed("part 1 v2")], [base_embed("part 2 v2")]]
    )
    assert _live(fake, row.channel_id) == first  # edited, not reposted
    assert fake.messages[(row.channel_id, first[1])][0].title == "part 2 v2"


async def test_board_shrinking_back_to_one_message_deletes_the_second(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))
    await board.publish_messages(war_id, "targets", [[base_embed("a")], [base_embed("b")]])
    row = await _targets_row(database, war_id)
    first, second = _live(fake, row.channel_id)

    await board.publish(war_id, "targets", base_embed("all of it"))

    assert _live(fake, row.channel_id) == [first]
    assert (row.channel_id, second) in fake.removed
    row = await _targets_row(database, war_id)
    assert row.live_message_id == first and row.extra_message_ids is None


async def test_board_reposts_part_two_below_a_reposted_part_one(database: Database) -> None:
    """If someone deletes part 1, it comes back at the bottom of the channel — so part 2
    is reposted after it, or the board would read 2 then 1."""
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))
    await board.publish_messages(war_id, "targets", [[base_embed("a")], [base_embed("b")]])
    row = await _targets_row(database, war_id)
    old_first, old_second = _live(fake, row.channel_id)
    fake.delete_message(row.channel_id, old_first)

    await board.publish_messages(war_id, "targets", [[base_embed("a2")], [base_embed("b2")]])

    new_first, new_second = _live(fake, row.channel_id)
    assert old_second < new_first < new_second  # both new, part 1 above part 2
    assert (row.channel_id, old_second) in fake.removed
    assert fake.messages[(row.channel_id, new_first)][0].title == "a2"
    row = await _targets_row(database, war_id)
    assert (row.live_message_id, row.extra_message_ids) == (new_first, str(new_second))


async def test_moving_a_board_to_another_channel_forgets_its_second_message(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    await WarChannelManager(database, _gw(fake)).ensure_workspace(war_id)
    board = WarBoard(database, _gw(fake))
    await board.publish_messages(war_id, "targets", [[base_embed("a")], [base_embed("b")]])
    async with database.session() as session:
        await DiscordChannelRepository(session).upsert_channel(war_id, "targets", 424242)
    row = await _targets_row(database, war_id)
    assert row.live_message_id is None and row.extra_message_ids is None


def _big_targets(n: int) -> list[TargetView]:
    return [
        TargetView(
            player_id=i, name=f"Foe{i}", level=30, rank=i, score=0.5, priority="MEDIUM",
            state=PlayerState.OKAY, is_available=True, estimated_bs=1.0e9, fair_fight=1.5,
            status_description="Okay",
        )
        for i in range(1, n + 1)
    ]


async def test_publisher_splits_a_big_target_board_with_the_picker_under_part_two(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = DiscordWarPublisher(
        database,
        WarChannelManager(database, _gw(fake)),
        WarBoard(database, _gw(fake)),
        WarLogBoard(database, _gw(fake)),
    )
    await publisher.publish_war(war_id, _roster(war_id))
    targets = [*_big_targets(87), _abroad_view(500, "Japan")]
    await publisher.publish_targets(war_id, targets)

    row = await _targets_row(database, war_id)
    first, second = _live(fake, row.channel_id)
    assert fake.messages[(row.channel_id, first)][0].title == "⚔️ TARGET BOARD (1/2)"
    assert fake.messages[(row.channel_id, second)][0].title == "⚔️ TARGET BOARD (2/2)"
    assert not fake.views[(row.channel_id, first)].children  # no drop-down on part 1
    assert fake.views[(row.channel_id, second)].children  # the drop-down sits at the bottom

    # war over: the notice replaces the board and part 2 goes away
    await publisher.clear_targets(war_id)
    assert _live(fake, row.channel_id) == [first]
