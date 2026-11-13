"""End-of-war scoreboard: counting, layout, and the war-ends flow."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import select

from app.database.database import Database
from app.database.models import WarHit, WarLogEvent
from app.discord.gateway import GuildGateway
from app.discord.summary_embeds import war_summary_embeds, war_summary_messages
from app.discord.war_channels import WarBoard, WarChannelManager, WarLogBoard
from app.discord.war_publish import DiscordWarPublisher
from app.utils.time import utcnow
from app.war.detector import WarState
from app.war.hit_summary import (
    Outcome,
    outcome_of,
    summarise_hits,
    summary_csv,
    summary_filename,
)
from app.war.warlog import war_summary_event
from tests.conftest import FakeTornClient
from tests.fake_gateway import FakeGuildGateway
from tests.test_war_hits import _hits, _log_channel_id, _seed_war, _service
from tests.test_war_manager import (
    OUR_ID,
    _assessment,
    _log_events,
    _manager,
    _StubDetector,
    _war,
)
from tests.torn_fixtures import attack, attacks_page

T0 = datetime(2026, 9, 19, 18, 0, 0, tzinfo=UTC)


def _hit(
    hit_id: int,
    attacker_id: int | None,
    attacker: str | None,
    result: str,
    *,
    at: int | None = None,
) -> WarHit:
    return WarHit(
        id=hit_id,
        war_id=1,
        torn_attack_id=hit_id,
        started_at=T0 + timedelta(seconds=hit_id if at is None else at),
        attacker_id=attacker_id,
        attacker_name=attacker,
        result=result,
        is_war_hit=True,
        is_outgoing=True,
    )


# --- counting rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        ("Attacked", Outcome.WON),
        ("Hospitalized", Outcome.WON),
        ("Mugged", Outcome.WON),
        ("Arrested", Outcome.WON),
        ("Stalemate", Outcome.DRAW),
        ("Lost", Outcome.LOST),
        ("Assist", Outcome.ASSIST),
        ("Interrupted", Outcome.OTHER),
        ("Escape", Outcome.OTHER),  # ambiguous, so deliberately not guessed
        ("SomethingNew", Outcome.OTHER),
        ("  lost ", Outcome.LOST),
    ],
)
def test_result_strings_map_to_outcomes(result: str, outcome: Outcome) -> None:
    assert outcome_of(result) is outcome


def test_counts_each_players_won_draw_lost_assist_and_total() -> None:
    hits = [
        _hit(1, 10, "Alice", "Attacked"),
        _hit(2, 10, "Alice", "Hospitalized"),
        _hit(3, 10, "Alice", "Lost"),
        _hit(4, 10, "Alice", "Assist"),
        _hit(5, 10, "Alice", "Stalemate"),
        _hit(6, 10, "Alice", "Interrupted"),
        _hit(7, 20, "Bob", "Mugged"),
    ]
    alice, bob = summarise_hits(hits)

    assert (alice.name, alice.hits, alice.won, alice.draw, alice.lost, alice.assist) == (
        "Alice", 6, 2, 1, 1, 1,
    )
    assert (bob.name, bob.hits, bob.won, bob.draw, bob.lost, bob.assist) == ("Bob", 1, 1, 0, 0, 0)
    # Interrupted counts as a hit but not as W/D/L/A, so the columns need not add up.
    assert alice.won + alice.draw + alice.lost + alice.assist == alice.hits - 1


def test_busiest_first_then_by_name() -> None:
    hits = [
        _hit(1, 3, "zed", "Attacked"),
        _hit(2, 1, "Amy", "Attacked"),
        _hit(3, 2, "Bo", "Attacked"),
        _hit(4, 2, "Bo", "Attacked"),
    ]
    assert [p.name for p in summarise_hits(hits)] == ["Bo", "Amy", "zed"]


def test_a_renamed_player_is_one_row_under_the_newest_name() -> None:
    hits = [_hit(1, 5, "OldName", "Attacked"), _hit(2, 5, "NewName", "Attacked")]
    [only] = summarise_hits(hits)
    assert (only.name, only.hits) == ("NewName", 2)


def test_no_hits_means_no_rows() -> None:
    assert summarise_hits([]) == []


# --- layout --------------------------------------------------------------------


def _players(n: int):  # type: ignore[no-untyped-def]
    return summarise_hits([_hit(i, i, f"Player{i:03d}", "Attacked") for i in range(1, n + 1)])


def test_scoreboard_table_and_totals() -> None:
    hits = [
        _hit(1, 1, "LordZulu", "Attacked"),
        _hit(2, 1, "LordZulu", "Lost"),
        _hit(3, 2, "RatUser", "Assist"),
    ]
    [embed] = war_summary_embeds(
        our_faction_name="Null & Void",
        enemy_faction_name="Agents of Truth",
        result="WIN",
        players=summarise_hits(hits),
    )
    assert embed.title == "🏁 War summary · Null & Void vs Agents of Truth"
    text = embed.description or ""
    assert "**2** players · **3** hits · won **1** · draw **0** · lost **1** · assist **1**" in text
    assert "PLAYER" in text and "HITS" in text
    lord = next(line for line in text.splitlines() if line.startswith("LordZulu"))
    assert lord.split() == ["LordZulu", "2", "1", "0", "1", "0"]


def test_colour_follows_the_result() -> None:
    def colour(result: str | None) -> int:
        [e] = war_summary_embeds(
            our_faction_name="A", enemy_faction_name="B", result=result, players=_players(1)
        )
        return e.colour.value  # type: ignore[union-attr]

    assert colour("WIN") != colour("LOSS")
    assert colour("DRAW") == colour(None)  # neutral grey for anything else


def test_a_war_with_no_recorded_hits_says_so() -> None:
    [embed] = war_summary_embeds(
        our_faction_name="A", enemy_faction_name="B", result="WIN", players=[]
    )
    assert embed.description == "No hits were recorded for this war."


def test_a_full_100_player_faction_stays_within_discords_limits() -> None:
    messages = war_summary_messages(
        our_faction_name="A", enemy_faction_name="B", result="WIN", players=_players(100)
    )
    rows = 0
    for message in messages:
        assert len(message) <= 10
        assert sum(len(e) for e in message) <= 6000
        for embed in message:
            assert len(embed.description or "") <= 4096
            rows += sum(1 for line in (embed.description or "").splitlines()
                        if line.startswith("Player"))
    assert rows == 100  # nobody is dropped, however many pages it takes


# --- the war-ends flow -----------------------------------------------------------


async def test_finishing_a_war_queues_exactly_one_scoreboard(database: Database) -> None:
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector)
    await mgr.tick()
    detector.assessment = _assessment(WarState.WAR_FINISHED, _war(end=utcnow(), winner=OUR_ID))
    await mgr.tick()
    await mgr.tick()  # nothing new — must not queue a second one

    async with database.session() as session:
        war_id = (await session.scalars(select(WarLogEvent.war_id))).first()
    assert war_id is not None
    kinds = [e.kind for e in await _log_events(database, war_id)]
    assert kinds.count("WAR_SUMMARY") == 1


async def test_a_war_first_seen_already_finished_gets_no_scoreboard(database: Database) -> None:
    detector = _StubDetector(
        _assessment(WarState.WAR_FINISHED, _war(end=utcnow(), winner=OUR_ID))
    )
    await _manager(database, detector).tick()

    async with database.session() as session:
        kinds = list(await session.scalars(select(WarLogEvent.kind)))
    assert "WAR_SUMMARY" not in kinds


async def test_the_scoreboard_posts_after_the_hits_and_the_war_ended_line_and_only_once(
    database: Database,
) -> None:
    fake = FakeGuildGateway()
    gateway = cast(GuildGateway, fake)
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=2))
    await WarChannelManager(database, gateway).ensure_workspace(war_id)

    start_unix = int((utcnow() - timedelta(hours=1)).timestamp())
    us = (11, "Alice", OUR_ID, "Null & Void")
    them = (99, "EnemyGuy", 60001, "Rival Crew")
    other = (98, "Bystander", 31337, "Some Other Faction")
    await _service(database, FakeTornClient({r"/attacks": attacks_page(
        attack(1, start_unix + 1, attacker=us, defender=them, result="Hospitalized"),
        attack(2, start_unix + 2, attacker=us, defender=them, result="Lost", respect_gain=0),
        attack(3, start_unix + 3, attacker=(12, "Bob", OUR_ID, "Null & Void"), defender=them,
               result="Assist", respect_gain=0),
        attack(4, start_unix + 4, attacker=us, defender=other, is_ranked_war=False),  # outside
        attack(5, start_unix + 5, attacker=them, defender=us),  # incoming
    )})).sync(war_id)

    async with database.session() as session:
        from app.war.warlog import war_finished_event, war_summary_event
        session.add(war_finished_event(war_id, result="WIN"))
        session.add(war_summary_event(war_id))

    publisher = DiscordWarPublisher(
        database,
        WarChannelManager(database, gateway),
        WarBoard(database, gateway),
        WarLogBoard(database, gateway),
    )
    await publisher.publish_log(war_id)

    channel_id = await _log_channel_id(database, war_id)
    posted = sorted(
        (mid, embeds) for (cid, mid), embeds in fake.messages.items() if cid == channel_id
    )
    titles = [[e.title for e in embeds] for _, embeds in posted]
    # hits first, then the "war ended" line, then the scoreboard.
    assert titles[0][0].startswith("🟢 War hits")  # type: ignore[union-attr]
    assert "The war has won" in (posted[1][1][0].description or "")
    assert (posted[2][1][0].title or "").startswith("🏁 War summary")

    board = (posted[2][1][0].description or "")
    assert "**2** players · **3** hits · won **1** · draw **0** · lost **1** · assist **1**" in board
    assert "Bystander" not in board and "EnemyGuy" not in board  # only our war hits

    # The spreadsheet rides on the scoreboard message only — not on hits or the ended line.
    [(file_key, files)] = list(fake.attachments.items())
    assert file_key == (channel_id, posted[2][0])
    assert [n.split("-null-")[0] for n, _ in files] == ["war-summary", "war-hits"]
    name, content = files[0]
    assert name.startswith("war-summary-null-void-vs-rival-crew-") and name.endswith(".csv")
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0][:3] == ["Player", "Player ID", "Hits"]
    assert rows[1] == ["Alice", "11", "2", "1", "0", "1", "0", "0"]
    assert rows[2] == ["Bob", "12", "1", "0", "0", "0", "1", "0"]
    assert len(rows) == 3  # nobody else, and no outside or incoming hits

    # ...and the paste-ready raw hits: all 4 of our hits (war AND outside), never incoming.
    hit_rows = list(csv.reader(io.StringIO(files[1][1].decode("utf-8-sig"))))
    assert hit_rows[0] == ["Attacker", "Defender", "Result", "Respect", "#Chain", "RW?", "Timestamp"]
    assert len(hit_rows) == 1 + 4
    assert {r[1] for r in hit_rows[1:]} == {"EnemyGuy", "Bystander"}
    assert hit_rows[1][5] == "no"  # newest first: the outside hit

    before = len(fake.messages)
    await publisher.publish_log(war_id)  # a second pass must not re-post anything
    assert len(fake.messages) == before
    assert all(h.posted_message_id is not None for h in await _hits(database, war_id)
               if h.is_outgoing)


# --- the downloadable spreadsheet ----------------------------------------------------


def _csv_rows(players):  # type: ignore[no-untyped-def]
    return list(csv.reader(io.StringIO(summary_csv(players).decode("utf-8-sig"))))


def test_csv_has_a_row_per_player_and_other_makes_the_columns_add_up() -> None:
    hits = [
        _hit(1, 10, "Alice", "Attacked"),
        _hit(2, 10, "Alice", "Hospitalized"),
        _hit(3, 10, "Alice", "Lost"),
        _hit(4, 10, "Alice", "Assist"),
        _hit(5, 10, "Alice", "Stalemate"),
        _hit(6, 10, "Alice", "Interrupted"),
        _hit(7, 20, "Bob", "Mugged"),
    ]
    rows = _csv_rows(summarise_hits(hits))

    assert rows[0] == ["Player", "Player ID", "Hits", "Won", "Draw", "Lost", "Assist", "Other"]
    assert rows[1] == ["Alice", "10", "6", "2", "1", "1", "1", "1"]
    assert rows[2] == ["Bob", "20", "1", "1", "0", "0", "0", "0"]
    for row in rows[1:]:  # unlike the Discord table, the file always adds up
        assert sum(int(x) for x in row[3:]) == int(row[2])


def test_csv_starts_with_a_bom_so_excel_reads_it_correctly() -> None:
    assert summary_csv([]).startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("name", ['=HYPERLINK("http://x")', "+1", "-2", "@SUM(A1)"])
def test_csv_neutralises_names_that_a_spreadsheet_would_run_as_a_formula(name: str) -> None:
    rows = _csv_rows(summarise_hits([_hit(1, 5, name, "Attacked")]))
    assert rows[1][0] == "'" + name


def test_csv_leaves_the_id_blank_when_torn_gave_none() -> None:
    rows = _csv_rows(summarise_hits([_hit(1, None, "Ghost", "Attacked")]))
    assert rows[1][:2] == ["Ghost", ""]


def test_filename_names_both_factions_and_the_date() -> None:
    assert (
        summary_filename("Null & Void", "Agents of Truth", T0)
        == "war-summary-null-void-vs-agents-of-truth-2026-09-19.csv"
    )
    assert summary_filename(None, None, T0) == "war-summary-us-vs-enemy-2026-09-19.csv"


def test_filename_stays_short_for_very_long_faction_names() -> None:
    name = summary_filename("A" * 80, "B" * 80, T0)
    assert len(name) < 100 and name.endswith(".csv")


async def test_a_scoreboard_with_no_hits_attaches_no_file(database: Database) -> None:
    fake = FakeGuildGateway()
    gateway = cast(GuildGateway, fake)
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=2))
    await WarChannelManager(database, gateway).ensure_workspace(war_id)
    async with database.session() as session:
        session.add(war_summary_event(war_id))

    assert await WarLogBoard(database, gateway).publish_pending(war_id) == 1
    assert fake.attachments == {}


async def test_the_summary_lists_the_enemy_overdoses_seen_this_war(database: Database) -> None:
    from app.database.models import Overdose, Player

    fake = FakeGuildGateway()
    gateway = cast(GuildGateway, fake)
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=2))
    await WarChannelManager(database, gateway).ensure_workspace(war_id)
    async with database.session() as session:
        seen, earlier = Player(torn_player_id=801, name="Druggo"), Player(torn_player_id=802)
        session.add_all([seen, earlier])
        await session.flush()
        for player, before in ((seen, False), (earlier, True)):
            session.add(
                Overdose(
                    war_id=war_id,
                    player_id=player.id,
                    drug="Xanax",
                    first_seen_at=utcnow(),
                    before_watch=before,
                )
            )
        session.add(war_summary_event(war_id))

    assert await WarLogBoard(database, gateway).publish_pending(war_id) == 1
    titles = [e.title or "" for embeds in fake.messages.values() for e in embeds]
    descriptions = [e.description or "" for embeds in fake.messages.values() for e in embeds]
    assert "🤢 Enemy overdoses this war (1)" in titles  # the pre-announcement one is left out
    assert any("🤢 Druggo — Xanax · seen <t:" in d for d in descriptions)


async def test_the_summary_has_no_overdose_box_when_there_were_none(database: Database) -> None:
    fake = FakeGuildGateway()
    gateway = cast(GuildGateway, fake)
    war_id = await _seed_war(database, war_start=utcnow() - timedelta(hours=2))
    await WarChannelManager(database, gateway).ensure_workspace(war_id)
    async with database.session() as session:
        session.add(war_summary_event(war_id))

    await WarLogBoard(database, gateway).publish_pending(war_id)
    titles = [e.title or "" for embeds in fake.messages.values() for e in embeds]
    assert not any("overdoses" in t for t in titles)
