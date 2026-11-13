from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import select

from app.database.database import Database
from app.database.models import Faction, War, WarLogEvent
from app.database.repositories import WarRepository
from app.torn.errors import TornAPITransientError
from app.torn.models import RankedWar, RankedWarFaction
from app.utils.time import utcnow
from app.war.detector import WarAssessment, WarState
from app.war.manager import WarManager, WarPublisher
from app.war.roster import EnemyRosterService, Roster, RosterDiff
from app.war.score import WarScore
from app.war.target_service import TargetingService, TargetView

OUR_ID = 10001
ENEMY_ID = 60001
GUILD_ID = 7


class _StubDetector:
    def __init__(self, assessment: WarAssessment) -> None:
        self.assessment = assessment
        self.calls = 0
        # Torn's full ranked-war list, for closing a war the bot missed the end of.
        self.torn_wars: list[RankedWar] = []
        self.torn_error: Exception | None = None
        self.list_calls = 0

    async def detect(self) -> WarAssessment:
        self.calls += 1
        return self.assessment

    async def ranked_wars(self) -> list[RankedWar]:
        self.list_calls += 1
        if self.torn_error is not None:
            raise self.torn_error
        return self.torn_wars


def _war(
    *, war_id: int = 49999, end: datetime | None = None, winner: int | None = None
) -> RankedWar:
    return RankedWar(
        war_id=war_id,
        start=utcnow() - timedelta(hours=1),
        end=end,
        target_score=5000,
        factions=(
            RankedWarFaction(OUR_ID, "Ours & Co", 1200),
            RankedWarFaction(ENEMY_ID, "Rival Crew", 900),
        ),
        winner_faction_id=winner,
    )


def _assessment(state: WarState, war: RankedWar | None) -> WarAssessment:
    return WarAssessment(state=state, war=war, our_faction_id=OUR_ID)


def _manager(
    database: Database,
    detector: _StubDetector,
    *,
    roster_service: object | None = None,
    targeting_service: object | None = None,
    publisher: object | None = None,
) -> WarManager:
    return WarManager(
        database,
        cast(object, detector),  # type: ignore[arg-type]
        guild_id=GUILD_ID,
        our_faction_id=OUR_ID,
        roster_service=cast(EnemyRosterService, roster_service) if roster_service else None,
        targeting_service=(
            cast(TargetingService, targeting_service) if targeting_service else None
        ),
        publisher=cast(WarPublisher, publisher) if publisher else None,
    )


async def _wars(db: Database) -> list[War]:
    async with db.session() as session:
        return list(await session.scalars(select(War)))


async def test_no_war_creates_no_rows(database: Database) -> None:
    mgr = _manager(database, _StubDetector(_assessment(WarState.NO_WAR, None)))
    await mgr.tick()
    assert await _wars(database) == []


async def test_active_war_registered_exactly_once(database: Database) -> None:
    mgr = _manager(database, _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())))

    await mgr.tick()
    await mgr.tick()
    await mgr.tick()

    wars = await _wars(database)
    assert len(wars) == 1
    assert wars[0].guild_id == GUILD_ID
    assert wars[0].torn_war_id == 49999
    assert wars[0].status == "WAR_ACTIVE"
    assert wars[0].enemy_faction_name == "Rival Crew"

    async with database.session() as session:
        factions = list(await session.scalars(select(Faction)))
    assert {f.torn_faction_id: f.is_ours for f in factions} == {OUR_ID: True, ENEMY_ID: False}


async def test_two_servers_can_track_the_same_torn_war(database: Database) -> None:
    war = _war()
    await _manager(database, _StubDetector(_assessment(WarState.WAR_ACTIVE, war))).tick()
    other = WarManager(
        database,
        cast(object, _StubDetector(_assessment(WarState.WAR_ACTIVE, war))),  # type: ignore[arg-type]
        guild_id=GUILD_ID + 1,
        our_faction_id=OUR_ID,
    )
    await other.tick()
    wars = await _wars(database)
    assert {w.guild_id for w in wars} == {GUILD_ID, GUILD_ID + 1}
    assert all(w.torn_war_id == 49999 for w in wars)


async def test_active_then_finished_updates_same_row_with_result(database: Database) -> None:
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector)
    await mgr.tick()

    detector.assessment = _assessment(
        WarState.WAR_FINISHED, _war(end=utcnow() - timedelta(minutes=5), winner=OUR_ID)
    )
    await mgr.tick()

    wars = await _wars(database)
    assert len(wars) == 1
    assert wars[0].status == "WAR_FINISHED"
    assert wars[0].result == "WIN"
    assert wars[0].war_end is not None


async def test_first_sighting_already_finished_goes_straight_to_history(database: Database) -> None:
    war = _war(end=utcnow() - timedelta(hours=2), winner=ENEMY_ID)
    await _manager(database, _StubDetector(_assessment(WarState.WAR_FINISHED, war))).tick()

    wars = await _wars(database)
    assert len(wars) == 1
    assert wars[0].status == "WAR_FINISHED"
    assert wars[0].result == "LOSS"


class _StubRoster:
    async def build(
        self, war_id: int, enemy_faction_id: int, *, enemy_faction_name: str | None = None
    ) -> Roster:
        return Roster(
            war_id=war_id,
            enemy_faction_id=enemy_faction_id,
            enemy_faction_name=enemy_faction_name,
        )


class _StubPublisher:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.log_calls: list[int] = []
        self.target_calls: list[int] = []
        self.clear_target_calls: list[int] = []
        self.score_calls: list[tuple[int, WarScore]] = []
        self.score_accepted = True  # what publish_score reports: on Discord, or deferred

    async def publish_war(self, war_row_id: int, roster: Roster) -> None:
        self.calls.append(war_row_id)

    async def publish_targets(self, war_row_id: int, targets: list[TargetView]) -> None:
        self.target_calls.append(war_row_id)

    async def clear_targets(self, war_row_id: int) -> None:
        self.clear_target_calls.append(war_row_id)

    async def publish_log(self, war_row_id: int) -> None:
        self.log_calls.append(war_row_id)

    async def publish_score(self, war_row_id: int, score: WarScore) -> bool:
        self.score_calls.append((war_row_id, score))
        return self.score_accepted


async def test_publisher_invoked_once_for_a_new_active_war(database: Database) -> None:
    publisher = _StubPublisher()
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())),
        roster_service=_StubRoster(),
        publisher=publisher,
    )
    await mgr.tick()
    await mgr.tick()  # war already known — no re-publish
    assert len(publisher.calls) == 1


async def test_publisher_not_invoked_for_already_finished_war(database: Database) -> None:
    publisher = _StubPublisher()
    war = _war(end=utcnow() - timedelta(hours=1), winner=OUR_ID)
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_FINISHED, war)),
        roster_service=_StubRoster(),
        publisher=publisher,
    )
    await mgr.tick()
    assert publisher.calls == []


# --- live score box ------------------------------------------------------------


async def test_score_is_published_on_every_tick_of_an_active_war(database: Database) -> None:
    publisher = _StubPublisher()
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector, roster_service=_StubRoster(), publisher=publisher)

    await mgr.tick()
    await mgr.tick()  # nothing else changed, but the score may have

    war = (await _wars(database))[0]
    assert [war_id for war_id, _ in publisher.score_calls] == [war.id, war.id]
    score = publisher.score_calls[0][1]
    assert (score.our_score, score.enemy_score, score.target_score) == (1200, 900, 5000)
    assert score.finished is False


async def test_score_is_not_published_before_the_war_starts(database: Database) -> None:
    publisher = _StubPublisher()
    mgr = _manager(
        database, _StubDetector(_assessment(WarState.WAR_DETECTED, _war())), publisher=publisher
    )
    await mgr.tick()
    assert publisher.score_calls == []


async def test_a_score_failure_does_not_crash_the_tick(database: Database) -> None:
    class _BrokenPublisher(_StubPublisher):
        async def publish_score(self, war_row_id: int, score: WarScore) -> bool:
            raise RuntimeError("boom")

    publisher = _BrokenPublisher()
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())),
        roster_service=_StubRoster(),
        publisher=publisher,
    )
    await mgr.tick()  # must not raise
    assert publisher.calls  # the roster was still published


async def test_final_score_is_published_once_when_the_war_ends(database: Database) -> None:
    publisher = _StubPublisher()
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector, roster_service=_StubRoster(), publisher=publisher)
    await mgr.tick()
    publisher.score_calls.clear()

    detector.assessment = _assessment(
        WarState.WAR_FINISHED, _war(end=utcnow() - timedelta(minutes=1), winner=OUR_ID)
    )
    await mgr.tick()  # the tick that sees it end
    await mgr.tick()  # later ticks of the same finished war
    await mgr.tick()

    assert len(publisher.score_calls) == 1
    final = publisher.score_calls[0][1]
    assert final.finished is True
    assert final.result == "WIN"


async def test_final_score_is_retried_until_discord_accepts_it(database: Database) -> None:
    publisher = _StubPublisher()
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector, roster_service=_StubRoster(), publisher=publisher)
    await mgr.tick()
    publisher.score_calls.clear()

    detector.assessment = _assessment(
        WarState.WAR_FINISHED, _war(end=utcnow() - timedelta(minutes=1), winner=OUR_ID)
    )
    publisher.score_accepted = False  # Discord not ready
    await mgr.tick()
    await mgr.tick()
    assert len(publisher.score_calls) == 2  # kept trying

    publisher.score_accepted = True
    await mgr.tick()  # accepted this time...
    await mgr.tick()  # ...so no further attempts
    assert len(publisher.score_calls) == 3


async def test_a_war_first_seen_already_finished_gets_no_score_box(database: Database) -> None:
    publisher = _StubPublisher()
    war = _war(end=utcnow() - timedelta(hours=2), winner=ENEMY_ID)
    mgr = _manager(
        database, _StubDetector(_assessment(WarState.WAR_FINISHED, war)), publisher=publisher
    )
    await mgr.tick()
    await mgr.tick()
    assert publisher.score_calls == []


# --- war-log events (Phase 5, spec §32) -------------------------------------


async def _log_events(db: Database, war_id: int) -> list[WarLogEvent]:
    async with db.session() as session:
        return list(
            await session.scalars(
                select(WarLogEvent).where(WarLogEvent.war_id == war_id).order_by(WarLogEvent.id)
            )
        )


async def test_new_war_writes_a_registered_log_event(database: Database) -> None:
    mgr = _manager(database, _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())))
    await mgr.tick()

    war = (await _wars(database))[0]
    events = await _log_events(database, war.id)
    assert [e.kind for e in events] == ["WAR_REGISTERED"]


async def test_state_transition_writes_a_state_change_event_and_publishes_log(
    database: Database,
) -> None:
    publisher = _StubPublisher()
    detector = _StubDetector(_assessment(WarState.WAR_DETECTED, _war()))
    mgr = _manager(database, detector, publisher=publisher)
    await mgr.tick()  # registers the war (WAR_DETECTED)

    detector.assessment = _assessment(WarState.WAR_ACTIVE, _war())
    await mgr.tick()  # DETECTED -> ACTIVE

    war = (await _wars(database))[0]
    events = await _log_events(database, war.id)
    assert [e.kind for e in events] == ["WAR_REGISTERED", "STATE_CHANGE"]
    assert publisher.log_calls == [war.id]


async def test_war_finishing_writes_a_finished_event_not_a_generic_state_change(
    database: Database,
) -> None:
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector)
    await mgr.tick()

    detector.assessment = _assessment(
        WarState.WAR_FINISHED, _war(end=utcnow(), winner=OUR_ID)
    )
    await mgr.tick()

    war = (await _wars(database))[0]
    events = await _log_events(database, war.id)
    # The scoreboard marker follows the "war ended" line, in that order.
    assert [e.kind for e in events] == ["WAR_REGISTERED", "WAR_FINISHED", "WAR_SUMMARY"]
    assert "won" in events[1].message


async def test_war_finishing_clears_the_target_board_exactly_once(database: Database) -> None:
    publisher = _StubPublisher()
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector, publisher=publisher)
    await mgr.tick()
    assert publisher.clear_target_calls == []  # still active: nothing to clear yet

    detector.assessment = _assessment(WarState.WAR_FINISHED, _war(end=utcnow(), winner=OUR_ID))
    await mgr.tick()
    await mgr.tick()  # already finished — must not clear a second time

    war = (await _wars(database))[0]
    assert publisher.clear_target_calls == [war.id]


async def test_unchanged_tick_writes_no_log_event(database: Database) -> None:
    mgr = _manager(database, _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())))
    await mgr.tick()
    await mgr.tick()  # nothing changed

    war = (await _wars(database))[0]
    events = await _log_events(database, war.id)
    assert [e.kind for e in events] == ["WAR_REGISTERED"]


# --- refresh_active (Phase 5) ------------------------------------------------


class _StubDiffRoster:
    def __init__(self, diff: RosterDiff) -> None:
        self.diff = diff
        self.calls: list[tuple[int, bool]] = []

    async def refresh_with_diff(self, war_id: int, *, fetch_estimates: bool = True) -> RosterDiff:
        self.calls.append((war_id, fetch_estimates))
        return self.diff


async def _active_war(database: Database) -> int:
    mgr = _manager(database, _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())))
    await mgr.tick()
    war = (await _wars(database))[0]
    return war.id


async def test_refresh_active_is_a_noop_with_no_open_war(database: Database) -> None:
    roster = _StubDiffRoster(RosterDiff(roster=Roster(war_id=1, enemy_faction_id=ENEMY_ID), joined=[], left=[]))
    mgr = _manager(
        database, _StubDetector(_assessment(WarState.NO_WAR, None)), roster_service=roster
    )
    result = await mgr.refresh_active()
    assert result is None
    assert roster.calls == []


async def test_refresh_active_publishes_and_logs_roster_changes(database: Database) -> None:
    war_id = await _active_war(database)
    diff = RosterDiff(
        roster=Roster(war_id=war_id, enemy_faction_id=ENEMY_ID),
        joined=[(111, "NewEnemy")],
        left=[(222, "GoneEnemy")],
    )
    roster = _StubDiffRoster(diff)
    publisher = _StubPublisher()
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())),
        roster_service=roster,
        publisher=publisher,
    )

    result = await mgr.refresh_active(include_stats=False)

    assert result is diff.roster
    assert roster.calls == [(war_id, False)]
    assert publisher.calls == [war_id]
    assert publisher.log_calls == [war_id]

    events = await _log_events(database, war_id)
    kinds = [e.kind for e in events]
    assert "ROSTER_JOIN" in kinds
    assert "ROSTER_LEFT" in kinds


# --- targeting wiring (Phase 6) ----------------------------------------------


class _StubTargeting:
    def __init__(self) -> None:
        self.calls: list[tuple[int, Roster]] = []

    async def refresh(self, war_id: int, roster: Roster) -> list[TargetView]:
        self.calls.append((war_id, roster))
        return []


async def test_new_war_registration_computes_and_publishes_targets(database: Database) -> None:
    targeting = _StubTargeting()
    publisher = _StubPublisher()
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())),
        roster_service=_StubRoster(),
        targeting_service=targeting,
        publisher=publisher,
    )
    await mgr.tick()

    war = (await _wars(database))[0]
    assert [war_id for war_id, _ in targeting.calls] == [war.id]
    assert publisher.target_calls == [war.id]


async def test_refresh_active_computes_and_publishes_targets(database: Database) -> None:
    war_id = await _active_war(database)
    diff = RosterDiff(roster=Roster(war_id=war_id, enemy_faction_id=ENEMY_ID), joined=[], left=[])
    targeting = _StubTargeting()
    publisher = _StubPublisher()
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())),
        roster_service=_StubDiffRoster(diff),
        targeting_service=targeting,
        publisher=publisher,
    )

    await mgr.refresh_active()

    assert [war_id for war_id, _ in targeting.calls] == [war_id]
    assert publisher.target_calls == [war_id]


async def test_no_targeting_service_means_no_publish_targets_call(database: Database) -> None:
    publisher = _StubPublisher()
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())),
        roster_service=_StubRoster(),
        publisher=publisher,
    )
    await mgr.tick()
    assert publisher.target_calls == []


async def test_targeting_failure_does_not_crash_the_tick(database: Database) -> None:
    class _BrokenTargeting:
        async def refresh(self, war_id: int, roster: Roster) -> list[TargetView]:
            raise RuntimeError("boom")

    publisher = _StubPublisher()
    mgr = _manager(
        database,
        _StubDetector(_assessment(WarState.WAR_ACTIVE, _war())),
        roster_service=_StubRoster(),
        targeting_service=_BrokenTargeting(),
        publisher=publisher,
    )
    await mgr.tick()  # must not raise
    assert publisher.calls  # the roster was still published
    assert publisher.target_calls == []


# --- closing a war the bot missed the end of --------------------------------
# The detector follows Torn's *newest* war. If the bot is down when a war ends and
# the next one is announced before it is back, the old row used to stay open
# forever (and get_active picked between the two at random).

OLD_WAR = 49999
NEW_WAR = 50000


async def _with_open_old_war(
    database: Database, publisher: _StubPublisher | None = None
) -> tuple[WarManager, _StubDetector]:
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war(war_id=OLD_WAR)))
    mgr = _manager(database, detector, publisher=publisher)
    await mgr.tick()
    # ...bot offline while it ends; next time it looks, Torn's newest war is another one.
    detector.assessment = _assessment(WarState.WAR_ACTIVE, _war(war_id=NEW_WAR))
    return mgr, detector


async def _by_torn_id(db: Database) -> dict[int, War]:
    return {w.torn_war_id: w for w in await _wars(db)}


async def test_a_missed_war_end_is_closed_with_torns_real_result(database: Database) -> None:
    publisher = _StubPublisher()
    mgr, detector = await _with_open_old_war(database, publisher)
    detector.torn_wars = [
        _war(war_id=NEW_WAR),
        _war(war_id=OLD_WAR, end=utcnow() - timedelta(hours=3), winner=OUR_ID),
    ]

    await mgr.tick()

    wars = await _by_torn_id(database)
    assert wars[OLD_WAR].status == "WAR_FINISHED"
    assert wars[OLD_WAR].result == "WIN"
    assert wars[OLD_WAR].war_end is not None
    assert wars[NEW_WAR].status == "WAR_ACTIVE"
    events = await _log_events(database, wars[OLD_WAR].id)
    assert [e.kind for e in events] == ["WAR_REGISTERED", "WAR_FINISHED", "WAR_SUMMARY"]
    # Same wrap-up as a war we watched end: board cleared, war-log posted.
    assert wars[OLD_WAR].id in publisher.clear_target_calls
    assert wars[OLD_WAR].id in publisher.log_calls
    async with database.session() as session:
        active = await WarRepository(session).get_active(GUILD_ID)
    assert active is not None and active.torn_war_id == NEW_WAR


async def test_a_missed_war_torn_no_longer_lists_is_closed_as_unknown(
    database: Database,
) -> None:
    mgr, detector = await _with_open_old_war(database)
    detector.torn_wars = [_war(war_id=NEW_WAR)]  # Torn has wars, but not the old one

    await mgr.tick()

    old = (await _by_torn_id(database))[OLD_WAR]
    assert old.status == "WAR_FINISHED"
    assert old.result is None
    assert old.war_end is not None
    events = await _log_events(database, old.id)
    assert [e.kind for e in events] == ["WAR_REGISTERED", "WAR_FINISHED", "WAR_SUMMARY"]
    assert events[1].message == "The war has ended."


async def test_closing_a_missed_war_happens_once(database: Database) -> None:
    mgr, detector = await _with_open_old_war(database)
    detector.torn_wars = [_war(war_id=NEW_WAR)]
    await mgr.tick()
    await mgr.tick()

    old = (await _by_torn_id(database))[OLD_WAR]
    assert len(await _log_events(database, old.id)) == 3  # no repeat finished/summary
    assert detector.list_calls == 1  # nothing stale left, so no extra Torn call


async def test_an_empty_torn_list_never_closes_a_war(database: Database) -> None:
    # An empty list is far more likely a Torn glitch than a real end.
    mgr, detector = await _with_open_old_war(database)
    detector.assessment = _assessment(WarState.NO_WAR, None)
    detector.torn_wars = []

    await mgr.tick()

    assert (await _by_torn_id(database))[OLD_WAR].status == "WAR_ACTIVE"


async def test_a_torn_failure_never_closes_a_war_or_breaks_the_tick(database: Database) -> None:
    mgr, detector = await _with_open_old_war(database)
    detector.torn_error = TornAPITransientError("backend down")

    await mgr.tick()  # must not raise

    wars = await _by_torn_id(database)
    assert wars[OLD_WAR].status == "WAR_ACTIVE"
    assert wars[NEW_WAR].status == "WAR_ACTIVE"  # the new war is still registered
    async with database.session() as session:
        active = await WarRepository(session).get_active(GUILD_ID)
    assert active is not None and active.torn_war_id == NEW_WAR  # newest wins, not random


async def test_a_war_torn_says_is_still_running_is_left_open(database: Database) -> None:
    mgr, detector = await _with_open_old_war(database)
    detector.torn_wars = [_war(war_id=NEW_WAR), _war(war_id=OLD_WAR)]  # no end, no winner

    await mgr.tick()

    assert (await _by_torn_id(database))[OLD_WAR].status == "WAR_ACTIVE"


async def test_no_extra_torn_call_when_nothing_is_stale(database: Database) -> None:
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))
    mgr = _manager(database, detector)
    await mgr.tick()
    await mgr.tick()
    assert detector.list_calls == 0
