"""Hit sync (``WarHitService``), posting (``WarLogBoard``) and manager wiring."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import discord
import pytest
from sqlalchemy import select

from app.database.database import Database
from app.database.models import DiscordChannel, Faction, War, WarHit, WarLogEvent
from app.discord.gateway import GuildGateway
from app.discord.hit_embeds import OUTSIDE_HIT_COLOUR, WAR_HIT_COLOUR
from app.discord.war_channels import WarChannelManager, WarLogBoard
from app.torn.attacks import AttackService
from app.torn.client import TornClient
from app.torn.errors import (
    TornAPIAccessLevelError,
    TornAPIPrivateDataError,
    TornAPIRateLimitError,
    TornAPIRequestError,
    error_for_code,
)
from app.utils.time import utcnow
from app.war.detector import WarState
from app.war.hits import WarHitService
from app.war.roster import Roster, RosterDiff
from app.war.warlog import HIT_FEED_BLOCKED_KIND
from tests.conftest import FakeTornClient
from tests.fake_gateway import FakeGuildGateway, discord_http_error
from tests.test_war_manager import (
    ENEMY_ID,
    OUR_ID,
    _assessment,
    _manager,
    _StubDetector,
    _StubDiffRoster,
    _StubPublisher,
    _war,
)
from tests.torn_fixtures import attack, attacks_page

UNIX_START = int((utcnow() - timedelta(hours=1)).timestamp())
THEM = (777, "EnemyGuy", ENEMY_ID, "Rival Crew")
US = (555, "OurGuy", OUR_ID, "Ours & Co")
RANDOM = (888, "Bystander", 31337, "Some Other Faction")


async def _seed_war(
    db: Database, *, war_start: datetime | None, status: str = "WAR_ACTIVE"
) -> int:
    async with db.session() as session:
        us = Faction(torn_faction_id=OUR_ID, name="Ours & Co", is_ours=True)
        them = Faction(torn_faction_id=ENEMY_ID, name="Rival Crew", is_ours=False)
        session.add_all([us, them])
        await session.flush()
        war = War(
            guild_id=99001,
            torn_war_id=77001,
            our_faction_id=us.id,
            our_faction_name="Ours & Co",
            enemy_faction_id=them.id,
            enemy_faction_name="Rival Crew",
            status=status,
            war_start=war_start,
        )
        session.add(war)
        await session.flush()
        return war.id


def _service(db: Database, fake: FakeTornClient) -> WarHitService:
    return WarHitService(db, AttackService(cast(TornClient, fake)), our_faction_id=OUR_ID)


async def _hits(db: Database, war_id: int) -> list[WarHit]:
    async with db.session() as session:
        return list(
            await session.scalars(
                select(WarHit).where(WarHit.war_id == war_id).order_by(WarHit.torn_attack_id)
            )
        )


def _from_param(path: str) -> int:
    return int(parse_qs(urlparse(path).query)["from"][0])


# --- WarHitService -----------------------------------------------------------


async def test_first_sync_starts_at_the_war_start_and_stores_both_directions(
    database: Database,
) -> None:
    start = utcnow() - timedelta(hours=1)
    war_id = await _seed_war(database, war_start=start)
    fake = FakeTornClient(
        {
            r"/attacks": attacks_page(
                attack(1, UNIX_START + 10, attacker=US, defender=THEM),
                attack(2, UNIX_START + 20, attacker=THEM, defender=US),
                attack(3, UNIX_START + 30, attacker=None, defender=US),  # stealthed on us
                attack(4, UNIX_START + 40, attacker=US, defender=RANDOM, is_ranked_war=False),
            )
        }
    )

    added = await _service(database, fake).sync(war_id)

    assert added == 4
    assert _from_param(fake.calls[0]) == int(start.timestamp())
    by_id = {h.torn_attack_id: h for h in await _hits(database, war_id)}
    assert by_id[1].is_outgoing and by_id[1].is_war_hit
    assert not by_id[2].is_outgoing
    assert not by_id[3].is_outgoing
    assert by_id[4].is_outgoing and not by_id[4].is_war_hit


async def test_next_sync_resumes_just_behind_the_newest_stored_hit_and_never_duplicates(
    database: Database,
) -> None:
    start = utcnow() - timedelta(hours=1)
    war_id = await _seed_war(database, war_start=start)
    newest = UNIX_START + 600
    fake = FakeTornClient(
        {r"/attacks": attacks_page(attack(1, UNIX_START + 5, attacker=US, defender=THEM),
                                   attack(2, newest, attacker=US, defender=THEM))}
    )
    service = _service(database, fake)
    assert await service.sync(war_id) == 2

    # Torn re-lists the overlap plus one genuinely new hit.
    fake2 = FakeTornClient(
        {r"/attacks": attacks_page(attack(2, newest, attacker=US, defender=THEM),
                                   attack(3, newest + 30, attacker=US, defender=THEM))}
    )
    assert await _service(database, fake2).sync(war_id) == 1

    assert 0 < newest - _from_param(fake2.calls[0]) <= 180  # the overlap, not the war start
    assert [h.torn_attack_id for h in await _hits(database, war_id)] == [1, 2, 3]


async def test_a_quiet_spell_on_our_side_still_advances_the_cursor(database: Database) -> None:
    """Hits made on us count towards the cursor, so we don't re-read them forever."""
    start = utcnow() - timedelta(hours=1)
    war_id = await _seed_war(database, war_start=start)
    incoming_at = UNIX_START + 900
    fake = FakeTornClient(
        {r"/attacks": attacks_page(attack(1, incoming_at, attacker=THEM, defender=US))}
    )
    await _service(database, fake).sync(war_id)

    fake2 = FakeTornClient({r"/attacks": attacks_page()})
    await _service(database, fake2).sync(war_id)
    assert incoming_at - _from_param(fake2.calls[0]) <= 180


async def test_a_pending_war_that_has_not_started_makes_no_request(database: Database) -> None:
    war_id = await _seed_war(
        database, war_start=utcnow() + timedelta(hours=2), status="WAR_DETECTED"
    )
    fake = FakeTornClient({})  # any call would raise
    assert await _service(database, fake).sync(war_id) == 0
    assert fake.calls == []


async def test_a_war_with_no_start_time_makes_no_request(database: Database) -> None:
    war_id = await _seed_war(database, war_start=None)
    fake = FakeTornClient({})
    assert await _service(database, fake).sync(war_id) == 0
    assert fake.calls == []


async def test_a_finished_war_only_asks_for_hits_up_to_its_end(database: Database) -> None:
    start = utcnow() - timedelta(hours=3)
    war_id = await _seed_war(database, war_start=start, status="WAR_FINISHED")
    end = utcnow() - timedelta(hours=1)
    async with database.session() as session:
        war = await session.get(War, war_id)
        assert war is not None
        war.war_end = end

    fake = FakeTornClient({r"/attacks": attacks_page()})
    await _service(database, fake).sync(war_id)
    assert parse_qs(urlparse(fake.calls[0]).query)["to"] == [str(int(end.timestamp()))]


async def test_failed_and_assist_hits_on_the_war_enemy_still_count_as_war_hits(
    database: Database,
) -> None:
    """Live data (2026-09-19): Torn's ranked-war flag is only set on hits that
    *scored* - 37 of 41 unflagged hits were losses / interrupts / assists on the
    enemy. Those must be war hits, not "outside" hits."""
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=1))
    fake = FakeTornClient(
        {
            r"/attacks": attacks_page(
                attack(1, UNIX_START + 1, attacker=US, defender=THEM, result="Lost",
                       respect_gain=0, is_ranked_war=False),
                attack(2, UNIX_START + 2, attacker=US, defender=THEM, result="Assist",
                       respect_gain=0, is_ranked_war=False),
                attack(3, UNIX_START + 3, attacker=US, defender=RANDOM, result="Mugged",
                       is_ranked_war=False),
            )
        }
    )
    await _service(database, fake).sync(war_id)

    by_id = {h.torn_attack_id: h for h in await _hits(database, war_id)}
    assert by_id[1].is_war_hit
    assert by_id[2].is_war_hit
    assert not by_id[3].is_war_hit  # a genuinely outside hit


async def test_an_unknown_enemy_falls_back_to_torns_flag_alone(database: Database) -> None:
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=1))
    async with database.session() as session:  # the placeholder row _reconcile writes
        war = await session.get(War, war_id)
        assert war is not None
        enemy = await session.get(Faction, war.enemy_faction_id)
        assert enemy is not None
        enemy.torn_faction_id = 0

    fake = FakeTornClient(
        {
            r"/attacks": attacks_page(
                attack(1, UNIX_START + 1, attacker=US, defender=THEM, is_ranked_war=True),
                attack(2, UNIX_START + 2, attacker=US, defender=THEM, is_ranked_war=False),
            )
        }
    )
    await _service(database, fake).sync(war_id)

    by_id = {h.torn_attack_id: h for h in await _hits(database, war_id)}
    assert by_id[1].is_war_hit
    assert not by_id[2].is_war_hit


def _no_chain_number(attack_id: int) -> dict[str, Any]:
    row = attack(attack_id, UNIX_START + attack_id, attacker=US, defender=THEM, result="Assist")
    row["chain"] = None  # Torn gave no chain number at all
    row["modifiers"] = None  # ...and no modifiers, so no warlord bonus either
    return row


async def test_chain_and_warlord_are_stored_and_missing_ones_get_neutral_values(
    database: Database,
) -> None:
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=1))
    fake = FakeTornClient(
        {
            r"/attacks": attacks_page(
                attack(1, UNIX_START + 1, attacker=US, defender=THEM, chain=7, warlord=1.15),
                attack(2, UNIX_START + 2, attacker=US, defender=THEM, result="Lost", chain=0,
                       respect_gain=0, is_ranked_war=False),
                _no_chain_number(3),
            )
        }
    )
    await _service(database, fake).sync(war_id)

    by_id = {h.torn_attack_id: h for h in await _hits(database, war_id)}
    assert (by_id[1].chain, by_id[1].warlord) == (7, 1.15)
    assert (by_id[2].chain, by_id[2].warlord) == (0, 1.0)
    # never NULL for new rows: NULL means "legacy, backfill me"
    assert (by_id[3].chain, by_id[3].warlord) == (0, 1.0)


async def _seed_legacy_rows(db: Database, war_id: int, ids_and_offsets: list[tuple[int, int]]) -> None:
    """Rows as an older version stored them: no chain number, no warlord bonus."""
    async with db.session() as session:
        for attack_id, offset in ids_and_offsets:
            session.add(
                WarHit(
                    war_id=war_id,
                    torn_attack_id=attack_id,
                    started_at=utcnow() - timedelta(hours=1) + timedelta(seconds=offset),
                    result="Attacked",
                    is_war_hit=True,
                    is_outgoing=True,
                    posted_message_id=555,  # already posted to Discord
                )
            )


async def test_hits_stored_before_the_new_columns_existed_are_backfilled_oldest_first(
    database: Database,
) -> None:
    start = utcnow() - timedelta(hours=2)
    war_id = await _seed_war(database, war_start=start)
    hour_ago = int((utcnow() - timedelta(hours=1)).timestamp())
    await _seed_legacy_rows(database, war_id, [(1, 10), (2, 20), (3, 30)])

    fake = FakeTornClient(
        {
            r"/attacks": attacks_page(
                attack(1, hour_ago + 10, attacker=US, defender=THEM, chain=5, warlord=1.15),
                attack(2, hour_ago + 20, attacker=US, defender=THEM, chain=0, warlord=1.0),
                attack(3, hour_ago + 30, attacker=US, defender=THEM, chain=6, warlord=1.18),
                attack(4, hour_ago + 40, attacker=US, defender=THEM, chain=7, warlord=1.0),
            )
        }
    )
    added = await _service(database, fake).sync(war_id)

    assert added == 1  # only the genuinely new hit counts as new
    # it re-read from the oldest unflagged hit, not from the newest stored one
    assert 0 <= (hour_ago + 10) - _from_param(fake.calls[0]) <= 180
    by_id = {h.torn_attack_id: h for h in await _hits(database, war_id)}
    assert [by_id[i].chain for i in (1, 2, 3, 4)] == [5, 0, 6, 7]
    assert [by_id[i].warlord for i in (1, 2, 3, 4)] == [1.15, 1.0, 1.18, 1.0]
    # already-posted hits stay posted: backfilling must never cause a re-post
    assert all(by_id[i].posted_message_id == 555 for i in (1, 2, 3))
    assert by_id[4].posted_message_id is None


async def test_once_backfilled_the_sync_goes_back_to_following_the_newest_hit(
    database: Database,
) -> None:
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=2))
    hour_ago = int((utcnow() - timedelta(hours=1)).timestamp())
    await _seed_legacy_rows(database, war_id, [(1, 10)])
    rows = attacks_page(attack(1, hour_ago + 10, attacker=US, defender=THEM))
    service = _service(database, FakeTornClient({r"/attacks": rows}))
    await service.sync(war_id)  # backfills

    fake2 = FakeTornClient({r"/attacks": attacks_page()})
    await _service(database, fake2).sync(war_id)
    assert 0 <= (hour_ago + 10) - _from_param(fake2.calls[0]) <= 180  # newest hit - overlap


async def test_a_row_torn_never_returns_does_not_pin_the_sync_to_an_old_window_forever(
    database: Database,
) -> None:
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=3))
    old = int((utcnow() - timedelta(hours=2)).timestamp())
    async with database.session() as session:  # one legacy row, plus a newer flagged one
        session.add(WarHit(war_id=war_id, torn_attack_id=1, started_at=utcnow() - timedelta(hours=2),
                           result="Attacked", is_war_hit=True, is_outgoing=True))
        session.add(WarHit(war_id=war_id, torn_attack_id=2, started_at=utcnow() - timedelta(minutes=5),
                           result="Attacked", is_war_hit=True, is_outgoing=True, chain=3,
                           warlord=1.0))
    fake = FakeTornClient({r"/attacks": attacks_page()})  # Torn no longer returns row 1
    service = _service(database, fake)

    for _ in range(3):
        await service.sync(war_id)
    assert all(abs(_from_param(p) - (old - 120)) < 5 for p in fake.calls)  # stuck on the old row

    await service.sync(war_id)  # patience exhausted -> follows the newest hit again
    newest = int((utcnow() - timedelta(minutes=5)).timestamp())
    assert 0 <= newest - _from_param(fake.calls[-1]) <= 180


# --- WarLogBoard.publish_pending_hits ----------------------------------------


async def _log_channel_id(db: Database, war_id: int) -> int:
    async with db.session() as session:
        row = await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == "log"
            )
        )
    assert row is not None
    return row.channel_id


async def _store(db: Database, war_id: int, *rows: dict[str, Any]) -> None:
    fake = FakeTornClient({r"/attacks": attacks_page(*rows)})
    await _service(db, fake).sync(war_id)


async def _board(db: Database, fake: FakeGuildGateway) -> tuple[int, WarLogBoard]:
    war_id = await _seed_war(db, war_start=utcnow() - timedelta(hours=1))
    await WarChannelManager(db, cast(GuildGateway, fake)).ensure_workspace(war_id)
    return war_id, WarLogBoard(db, cast(GuildGateway, fake))


async def test_board_posts_our_hits_colour_coded_once_and_skips_incoming(
    database: Database,
) -> None:
    fake = FakeGuildGateway()
    war_id, board = await _board(database, fake)
    await _store(
        database,
        war_id,
        attack(1, UNIX_START + 10, attacker=US, defender=THEM),
        attack(2, UNIX_START + 20, attacker=US, defender=RANDOM, is_ranked_war=False),
        attack(3, UNIX_START + 30, attacker=THEM, defender=US),  # incoming: never posted
    )

    assert await board.publish_pending_hits(war_id) == 2

    channel_id = await _log_channel_id(database, war_id)
    assert fake.message_count(channel_id) == 1
    [embeds] = [e for (cid, _), e in fake.messages.items() if cid == channel_id]
    assert [e.colour for e in embeds] == [WAR_HIT_COLOUR, OUTSIDE_HIT_COLOUR]

    # Posted hits are marked, so a second pass sends nothing.
    assert await board.publish_pending_hits(war_id) == 0
    assert fake.message_count(channel_id) == 1
    incoming = next(h for h in await _hits(database, war_id) if h.torn_attack_id == 3)
    assert incoming.posted_message_id is None


async def test_board_leaves_hits_unposted_while_discord_is_not_ready(
    database: Database,
) -> None:
    fake = FakeGuildGateway()
    war_id, _ = await _board(database, fake)
    await _store(database, war_id, attack(1, UNIX_START + 10, attacker=US, defender=THEM))

    offline = FakeGuildGateway(ready=False)
    board = WarLogBoard(database, cast(GuildGateway, offline))
    assert await board.publish_pending_hits(war_id) == 0
    assert all(h.posted_message_id is None for h in await _hits(database, war_id))

    # Back online: the same hit is delivered, not lost.
    assert await WarLogBoard(database, cast(GuildGateway, fake)).publish_pending_hits(war_id) == 1


async def test_board_skips_a_hit_message_discord_rejects(database: Database) -> None:
    # A 400 can never succeed on retry; without a skip it would block every later hit.
    fake = FakeGuildGateway()
    war_id, board = await _board(database, fake)
    await _store(database, war_id, attack(1, UNIX_START + 10, attacker=US, defender=THEM))
    fake.send_failures = [discord_http_error(discord.HTTPException, 400)]

    assert await board.publish_pending_hits(war_id) == 0
    assert all(h.posted_message_id is not None for h in await _hits(database, war_id))

    await _store(database, war_id, attack(2, UNIX_START + 20, attacker=US, defender=THEM))
    assert await board.publish_pending_hits(war_id) == 1  # only the new hit
    assert fake.message_count(await _log_channel_id(database, war_id)) == 1


async def test_board_keeps_hits_queued_when_the_bot_lacks_permission(
    database: Database,
) -> None:
    fake = FakeGuildGateway()
    war_id, board = await _board(database, fake)
    await _store(database, war_id, attack(1, UNIX_START + 10, attacker=US, defender=THEM))
    fake.send_failures = [discord_http_error(discord.Forbidden, 403)]

    assert await board.publish_pending_hits(war_id) == 0
    assert all(h.posted_message_id is None for h in await _hits(database, war_id))
    assert await board.publish_pending_hits(war_id) == 1  # permission back: delivered


async def test_board_is_a_noop_without_a_log_channel(database: Database) -> None:
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=1))
    await _store(database, war_id, attack(1, UNIX_START + 10, attacker=US, defender=THEM))
    board = WarLogBoard(database, cast(GuildGateway, FakeGuildGateway()))
    assert await board.publish_pending_hits(war_id) == 0


async def test_a_large_backlog_is_posted_across_passes_without_repeats(
    database: Database,
) -> None:
    fake = FakeGuildGateway()
    war_id, board = await _board(database, fake)
    await _store(
        database,
        war_id,
        *(attack(i, UNIX_START + i, attacker=US, defender=THEM) for i in range(1, 101)),
    )
    async with database.session() as session:  # top up past one pass (400) cheaply
        for i in range(101, 501):
            session.add(
                WarHit(
                    war_id=war_id,
                    torn_attack_id=i,
                    started_at=utcnow() - timedelta(minutes=30) + timedelta(seconds=i),
                    result="Attacked",
                    is_war_hit=True,
                    is_outgoing=True,
                )
            )

    first = await board.publish_pending_hits(war_id)
    second = await board.publish_pending_hits(war_id)
    third = await board.publish_pending_hits(war_id)
    assert (first, second, third) == (400, 100, 0)


# --- manager wiring ----------------------------------------------------------


class _StubHits:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[int] = []

    async def sync(self, war_row_id: int) -> int:
        self.calls.append(war_row_id)
        if self.fail is not None:
            raise self.fail
        return 3


def _wire(
    database: Database,
    hits: _StubHits,
    *,
    state: WarState = WarState.WAR_ACTIVE,
    war: Any = None,
    roster: object | None = None,
    publisher: object | None = None,
) -> Any:
    mgr = _manager(
        database,
        _StubDetector(_assessment(state, war or _war())),
        roster_service=roster,
        publisher=publisher,
    )
    mgr._hit_service = hits  # type: ignore[assignment]  # stub stands in for WarHitService
    return mgr


def _roster_for(war_id: int) -> _StubDiffRoster:
    return _StubDiffRoster(
        RosterDiff(roster=Roster(war_id=war_id, enemy_faction_id=ENEMY_ID), joined=[], left=[])
    )


async def _open_war(database: Database) -> int:
    seed = _manager(database, _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())))
    await seed.tick()
    async with database.session() as session:
        return (await session.scalars(select(War.id))).one()


async def test_refresh_syncs_hits_then_publishes_the_log(database: Database) -> None:
    war_id = await _open_war(database)
    hits, publisher = _StubHits(), _StubPublisher()
    mgr = _wire(database, hits, roster=_roster_for(war_id), publisher=publisher)

    await mgr.refresh_active(include_stats=False)

    assert hits.calls == [war_id]
    assert publisher.log_calls == [war_id]


async def test_a_failing_hit_feed_never_stops_the_roster_refresh(database: Database) -> None:
    war_id = await _open_war(database)
    hits = _StubHits(fail=TornAPIAccessLevelError("no faction api access"))
    roster, publisher = _roster_for(war_id), _StubPublisher()
    mgr = _wire(database, hits, roster=roster, publisher=publisher)

    result = await mgr.refresh_active()  # must not raise

    assert result is not None
    assert roster.calls and publisher.calls == [war_id]


async def test_a_persistent_hit_feed_failure_is_logged_once_not_every_minute(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    war_id = await _open_war(database)
    hits = _StubHits(fail=TornAPIAccessLevelError("no faction api access"))
    mgr = _wire(database, hits, roster=_roster_for(war_id), publisher=_StubPublisher())

    for _ in range(3):
        await mgr.refresh_active()

    assert capsys.readouterr().out.count("war.hit_sync_failed") == 1
    assert len(hits.calls) == 3  # still retried each time, just not re-announced


async def test_the_war_ending_does_a_final_hit_sweep_before_the_log_posts(
    database: Database,
) -> None:
    war_id = await _open_war(database)
    hits, publisher = _StubHits(), _StubPublisher()
    finished = _war(end=utcnow(), winner=OUR_ID)
    mgr = _wire(database, hits, state=WarState.WAR_FINISHED, war=finished, publisher=publisher)

    await mgr.tick()

    assert hits.calls == [war_id]
    assert publisher.log_calls == [war_id]


async def test_a_war_first_seen_already_finished_is_not_backfilled(database: Database) -> None:
    hits, publisher = _StubHits(), _StubPublisher()
    finished = _war(end=utcnow(), winner=OUR_ID)
    mgr = _wire(database, hits, state=WarState.WAR_FINISHED, war=finished, publisher=publisher)

    await mgr.tick()

    assert hits.calls == []


# --- hit feed refused by Torn: one #war-log notice per war -------------------


async def _blocked_notices(database: Database, war_id: int) -> list[WarLogEvent]:
    async with database.session() as session:
        rows = await session.scalars(
            select(WarLogEvent).where(
                WarLogEvent.war_id == war_id, WarLogEvent.kind == HIT_FEED_BLOCKED_KIND
            )
        )
        return list(rows)


def test_torn_error_7_is_a_private_data_error() -> None:
    err = error_for_code(7, "Incorrect ID-entity relation")
    assert type(err) is TornAPIPrivateDataError
    assert isinstance(err, TornAPIRequestError)  # existing handlers still catch it


async def test_a_refused_hit_feed_posts_one_war_log_notice(database: Database) -> None:
    war_id = await _open_war(database)
    hits = _StubHits(fail=error_for_code(7, "Incorrect ID-entity relation"))
    publisher = _StubPublisher()
    mgr = _wire(database, hits, roster=_roster_for(war_id), publisher=publisher)

    for _ in range(3):
        await mgr.refresh_active()

    notices = await _blocked_notices(database, war_id)
    assert len(notices) == 1
    assert "Faction API Access" in notices[0].message
    assert "/setup torn-key" in notices[0].message
    assert notices[0].posted_message_id is None  # queued for #war-log
    assert war_id in publisher.log_calls


async def test_the_notice_is_not_repeated_after_a_restart(database: Database) -> None:
    war_id = await _open_war(database)
    for _ in range(2):  # a fresh manager = the bot restarted (in-memory state lost)
        hits = _StubHits(fail=error_for_code(7, "Incorrect ID-entity relation"))
        mgr = _wire(database, hits, roster=_roster_for(war_id), publisher=_StubPublisher())
        await mgr.refresh_active()

    assert len(await _blocked_notices(database, war_id)) == 1


async def test_a_key_with_too_low_access_also_posts_the_notice(database: Database) -> None:
    war_id = await _open_war(database)
    hits = _StubHits(fail=TornAPIAccessLevelError("Torn API error 16: Access level"))
    mgr = _wire(database, hits, roster=_roster_for(war_id), publisher=_StubPublisher())

    await mgr.refresh_active()

    notices = await _blocked_notices(database, war_id)
    assert len(notices) == 1
    assert "access level" in notices[0].message


async def test_a_temporary_torn_error_posts_no_notice(database: Database) -> None:
    war_id = await _open_war(database)
    hits = _StubHits(fail=TornAPIRateLimitError("Torn API error 5: Too many requests"))
    mgr = _wire(database, hits, roster=_roster_for(war_id), publisher=_StubPublisher())

    await mgr.refresh_active()

    assert await _blocked_notices(database, war_id) == []
