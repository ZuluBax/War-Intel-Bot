from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import func, select

from app.database.database import Database
from app.database.models import (
    Faction,
    FlightRecord,
    Player,
    PlayerStatusHistory,
    StatEstimate,
    War,
    WarPlayer,
)
from app.database.repositories import FlightRecordRepository, WarPlayerRepository
from app.ffscouter.client import FFScouterClient
from app.ffscouter.errors import FFScouterError
from app.ffscouter.models import FFScouterStat
from app.torn.faction import FactionService
from app.torn.models import FactionMember, FactionSummary, PlayerState, TornStatus
from app.war.roster import EnemyRosterService, Roster, RosterEntry

# --- pure Roster ordering ---------------------------------------------------


def _roster() -> Roster:
    return Roster(
        war_id=1,
        enemy_faction_id=999,
        entries=[
            RosterEntry(player_id=1, estimated_bs=1_000_000_000),
            RosterEntry(player_id=2, estimated_bs=4_000_000_000),
            RosterEntry(player_id=3, estimated_bs=None),
            RosterEntry(player_id=4, estimated_bs=2_000_000_000),
        ],
    )


def test_sorted_by_strength_descending_puts_unknown_last() -> None:
    assert [e.player_id for e in _roster().sorted_by_strength(descending=True)] == [2, 4, 1, 3]


def test_sorted_by_strength_ascending_puts_unknown_last() -> None:
    assert [e.player_id for e in _roster().sorted_by_strength(descending=False)] == [1, 4, 2, 3]


# --- EnemyRosterService.build --------------------------------------------


class _StubFaction:
    def __init__(self, members: list[FactionMember]) -> None:
        self._members = members

    async def get_members(self, faction_id: int) -> FactionSummary:
        return FactionSummary(faction_id=faction_id, members=tuple(self._members))


class _StubFFScouter:
    def __init__(self, stats: dict[int, FFScouterStat] | None, *, fail: bool = False) -> None:
        self._stats = stats or {}
        self._fail = fail
        self.calls = 0

    async def get_stats(self, ids: list[int]) -> dict[int, FFScouterStat]:
        self.calls += 1
        if self._fail:
            raise FFScouterError("FFScouter down")
        return {pid: self._stats.get(pid, FFScouterStat(player_id=pid)) for pid in ids}


def _member(pid: int, *, level: int, state: PlayerState = PlayerState.OKAY) -> FactionMember:
    return FactionMember(
        player_id=pid,
        name=f"Enemy{pid}",
        level=level,
        status=TornStatus(state=state, description=state.value),
    )


def _service(
    database: Database, members: list[FactionMember], ff: _StubFFScouter
) -> EnemyRosterService:
    return EnemyRosterService(
        database,
        cast(FactionService, _StubFaction(members)),
        cast(FFScouterClient, ff),
    )


async def _count(database: Database, model: type) -> int:
    async with database.session() as session:
        return int((await session.scalar(select(func.count()).select_from(model))) or 0)


async def test_build_persists_players_links_status_and_estimates(database: Database) -> None:
    members = [_member(101, level=40), _member(102, level=55, state=PlayerState.HOSPITAL)]
    ff = _StubFFScouter(
        {
            101: FFScouterStat(player_id=101, bs_estimate=1.2e9, bs_estimate_human="1.2b", fair_fight=1.9),
            102: FFScouterStat(player_id=102, bs_estimate=3.4e9, bs_estimate_human="3.4b", fair_fight=2.4),
        }
    )
    roster = await _service(database, members, ff).build(1, 777, enemy_faction_name="Rivals")

    assert roster.estimates_available is True
    assert roster.enemy_faction_name == "Rivals"
    assert await _count(database, Player) == 2
    assert await _count(database, WarPlayer) == 2
    assert await _count(database, StatEstimate) == 2
    assert await _count(database, PlayerStatusHistory) == 2

    top = roster.sorted_by_strength()[0]
    assert top.player_id == 102
    assert top.estimated_bs == 3.4e9
    assert top.estimate_is_fresh is True
    assert top.state is PlayerState.HOSPITAL


async def test_build_is_idempotent_for_players_but_appends_estimates(database: Database) -> None:
    members = [_member(201, level=30)]
    ff = _StubFFScouter({201: FFScouterStat(player_id=201, bs_estimate=9.0e8)})
    svc = _service(database, members, ff)

    await svc.build(1, 777)
    await svc.build(1, 777)

    assert await _count(database, Player) == 1
    assert await _count(database, WarPlayer) == 1
    assert await _count(database, StatEstimate) == 2  # append-only history


async def test_ffscouter_failure_keeps_previous_estimate(database: Database) -> None:
    members = [_member(301, level=45)]

    # First build succeeds and stores an estimate.
    ok = _StubFFScouter({301: FFScouterStat(player_id=301, bs_estimate=2.0e9, fair_fight=1.7)})
    await _service(database, members, ok).build(1, 777)

    # Second build: FFScouter is down.
    down = _StubFFScouter(None, fail=True)
    roster = await _service(database, members, down).build(1, 777)

    assert roster.estimates_available is False
    assert await _count(database, StatEstimate) == 1  # nothing appended, nothing deleted
    entry = roster.entries[0]
    assert entry.estimated_bs == 2.0e9  # served from the DB fallback
    assert entry.estimate_is_fresh is False


async def test_build_handles_player_with_no_estimate(database: Database) -> None:
    members = [_member(401, level=25)]
    ff = _StubFFScouter({401: FFScouterStat(player_id=401)})  # no bs_estimate
    roster = await _service(database, members, ff).build(1, 777)

    assert await _count(database, StatEstimate) == 0
    assert roster.entries[0].estimated_bs is None


@pytest.mark.parametrize("descending", [True, False])
async def test_roster_sort_direction(database: Database, descending: bool) -> None:
    members = [_member(1, level=10), _member(2, level=10)]
    ff = _StubFFScouter(
        {
            1: FFScouterStat(player_id=1, bs_estimate=1.0e9),
            2: FFScouterStat(player_id=2, bs_estimate=5.0e9),
        }
    )
    roster = await _service(database, members, ff).build(1, 777)
    order = [e.player_id for e in roster.sorted_by_strength(descending=descending)]
    assert order == ([2, 1] if descending else [1, 2])


# --- fetch_estimates=False (status-only refresh, Phase 5) -------------------


async def test_status_only_build_skips_ffscouter_but_keeps_last_estimate(
    database: Database,
) -> None:
    members = [_member(601, level=50)]
    ff = _StubFFScouter({601: FFScouterStat(player_id=601, bs_estimate=7.0e9)})
    svc = _service(database, members, ff)

    await svc.build(1, 777)  # fresh estimate stored
    roster = await svc.build(1, 777, fetch_estimates=False)

    assert ff.calls == 1  # the status-only pass never touched FFScouter
    assert roster.estimates_available is True  # not treated as an outage
    assert roster.entries[0].estimated_bs == 7.0e9  # served from the DB fallback
    assert roster.entries[0].estimate_is_fresh is False
    assert await _count(database, StatEstimate) == 1  # nothing appended


# --- refresh_with_diff (roster join/leave, Phase 5) --------------------------
# refresh_with_diff() resolves the enemy Torn faction ID from the wars/factions
# tables (like refresh()), so these tests seed a minimal War row rather than
# calling build() directly.


async def _seed_war(database: Database, *, enemy_torn_faction_id: int = 777) -> int:
    async with database.session() as session:
        our = Faction(torn_faction_id=1, name="Us", is_ours=True)
        enemy = Faction(torn_faction_id=enemy_torn_faction_id, name="Rivals", is_ours=False)
        session.add_all([our, enemy])
        await session.flush()
        war = War(
            guild_id=1,
            torn_war_id=999,
            our_faction_id=our.id,
            our_faction_name=our.name,
            enemy_faction_id=enemy.id,
            enemy_faction_name=enemy.name,
            status="WAR_ACTIVE",
        )
        session.add(war)
        await session.flush()
        return war.id


async def test_refresh_with_diff_reports_everyone_as_joined_with_no_prior_roster(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    members = [_member(701, level=20), _member(702, level=20)]
    svc = _service(database, members, _StubFFScouter({}))

    diff = await svc.refresh_with_diff(war_id, fetch_estimates=False)

    assert {e.player_id for e in diff.roster.entries} == {701, 702}
    assert {pid for pid, _ in diff.joined} == {701, 702}
    assert diff.left == []


async def test_refresh_with_diff_reports_no_change_when_roster_is_unchanged(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    members = [_member(703, level=20), _member(704, level=20)]
    svc = _service(database, members, _StubFFScouter({}))

    await svc.refresh_with_diff(war_id, fetch_estimates=False)  # establishes the baseline
    diff = await svc.refresh_with_diff(war_id, fetch_estimates=False)

    assert diff.joined == []
    assert diff.left == []


async def test_refresh_with_diff_detects_join_and_leave(database: Database) -> None:
    war_id = await _seed_war(database)
    svc_v1 = _service(
        database, [_member(801, level=20), _member(802, level=20)], _StubFFScouter({})
    )
    await svc_v1.refresh_with_diff(war_id, fetch_estimates=False)

    # 802 left the faction, 803 joined.
    svc_v2 = _service(
        database, [_member(801, level=20), _member(803, level=20)], _StubFFScouter({})
    )
    diff = await svc_v2.refresh_with_diff(war_id, fetch_estimates=False)

    assert diff.joined == [(803, "Enemy803")]
    assert diff.left == [(802, "Enemy802")]

    async with database.session() as session:
        active = await WarPlayerRepository(session).active_enemy_torn_ids(war_id)
    assert active == {801, 803}


async def test_rejoining_a_faction_clears_left_flag(database: Database) -> None:
    war_id = await _seed_war(database)
    only_901 = _service(database, [_member(901, level=20)], _StubFFScouter({}))
    both = _service(
        database, [_member(901, level=20), _member(902, level=20)], _StubFFScouter({})
    )

    await both.refresh_with_diff(war_id, fetch_estimates=False)  # 901, 902 join
    diff_left = await only_901.refresh_with_diff(war_id, fetch_estimates=False)  # 902 leaves
    assert diff_left.left == [(902, "Enemy902")]

    diff_rejoin = await both.refresh_with_diff(war_id, fetch_estimates=False)  # 902 rejoins
    assert diff_rejoin.joined == [(902, "Enemy902")]

    async with database.session() as session:
        active = await WarPlayerRepository(session).active_enemy_torn_ids(war_id)
    assert active == {901, 902}


# --- flight take-off tracking (drives the landing-time estimate) -----------------


def _flying(pid: int, *, plane: str | None = "light_aircraft") -> FactionMember:
    return FactionMember(
        player_id=pid,
        name=f"Enemy{pid}",
        level=40,
        status=TornStatus(
            state=PlayerState.TRAVELING,
            description="Traveling from Torn to Japan",
            plane_type=plane,
        ),
    )


_T = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
_GAP = timedelta(minutes=3)


def _link() -> WarPlayer:
    return WarPlayer(war_id=1, player_id=1)


def test_status_first_ever_look_has_no_known_start() -> None:
    link = _link()
    assert WarPlayerRepository.observe_status(link, "Traveling from Torn to Japan", now=_T, max_gap=_GAP) is None


def test_status_change_seen_soon_after_the_last_look_records_its_start() -> None:
    link = _link()
    WarPlayerRepository.observe_status(link, "Okay", now=_T, max_gap=_GAP)
    later = _T + timedelta(minutes=1)
    assert WarPlayerRepository.observe_status(link, "Traveling from Torn to Japan", now=later, max_gap=_GAP) == later


def test_status_start_is_kept_while_the_status_stays_the_same() -> None:
    link = _link()
    WarPlayerRepository.observe_status(link, "Okay", now=_T, max_gap=_GAP)
    took_off = _T + timedelta(minutes=1)
    WarPlayerRepository.observe_status(link, "Traveling from Torn to Japan", now=took_off, max_gap=_GAP)
    still = WarPlayerRepository.observe_status(
        link, "Traveling from Torn to Japan", now=took_off + timedelta(minutes=2), max_gap=_GAP
    )
    assert still == took_off


def test_status_change_after_a_long_gap_has_no_known_start() -> None:
    """Bot was down: the flight may have left any time in the gap, so don't pretend."""
    link = _link()
    WarPlayerRepository.observe_status(link, "Okay", now=_T, max_gap=_GAP)
    much_later = _T + timedelta(hours=2)
    assert WarPlayerRepository.observe_status(link, "Traveling from Torn to Japan", now=much_later, max_gap=_GAP) is None


def test_same_status_after_a_long_gap_is_not_trusted_either() -> None:
    """Identical words two hours apart could be a second identical flight."""
    link = _link()
    WarPlayerRepository.observe_status(link, "Okay", now=_T, max_gap=_GAP)
    WarPlayerRepository.observe_status(
        link, "Traveling from Torn to Japan", now=_T + timedelta(minutes=1), max_gap=_GAP
    )
    much_later = _T + timedelta(hours=3)
    assert WarPlayerRepository.observe_status(link, "Traveling from Torn to Japan", now=much_later, max_gap=_GAP) is None


async def test_build_carries_status_and_take_off_time_onto_roster_entries(database: Database) -> None:
    ff = _StubFFScouter({})
    faction = _StubFaction([_member(301, level=40)])
    svc = EnemyRosterService(
        database, cast(FactionService, faction), cast(FFScouterClient, ff)
    )
    await svc.build(1, 777)  # first look: grounded

    faction._members = [_flying(301)]  # now in the air
    roster = await svc.build(1, 777)

    entry = roster.entries[0]
    assert entry.status_description == "Traveling from Torn to Japan"
    assert entry.plane_type == "light_aircraft"
    assert entry.status_since is not None  # we watched it take off


async def test_build_does_not_invent_a_take_off_for_a_flight_already_in_the_air(database: Database) -> None:
    faction = _StubFaction([_flying(302)])
    svc = EnemyRosterService(
        database, cast(FactionService, faction), cast(FFScouterClient, _StubFFScouter({}))
    )
    roster = await svc.build(1, 777)  # very first look — already flying
    assert roster.entries[0].status_since is None


async def test_build_records_the_plane_type_in_status_history(database: Database) -> None:
    """Kept so the plane -> flight-speed factors can be checked against real flights."""
    svc = EnemyRosterService(
        database,
        cast(FactionService, _StubFaction([_flying(303, plane="private_jet")])),
        cast(FFScouterClient, _StubFFScouter({})),
    )
    await svc.build(1, 777)
    async with database.session() as session:
        row = await session.scalar(select(PlayerStatusHistory))
    assert row is not None
    assert row.plane_type == "private_jet"



async def test_build_carries_the_online_label_onto_entries_and_history(database: Database) -> None:
    online = FactionMember(
        player_id=401, name="Enemy401", level=40, online_status="Online",
        status=TornStatus(state=PlayerState.OKAY, description="Okay"),
    )
    svc = EnemyRosterService(
        database, cast(FactionService, _StubFaction([online])), cast(FFScouterClient, _StubFFScouter({}))
    )
    roster = await svc.build(1, 777)
    assert roster.entries[0].online_status == "Online"
    async with database.session() as session:
        row = await session.scalar(select(PlayerStatusHistory))
    assert row is not None
    assert row.online_status == "Online"


async def test_build_records_a_flight_watched_from_take_off_to_landing(
    database: Database,
) -> None:
    faction = _StubFaction([_member(303, level=40)])
    svc = EnemyRosterService(
        database, cast(FactionService, faction), cast(FFScouterClient, _StubFFScouter({}))
    )
    await svc.build(1, 777)  # grounded
    faction._members = [_flying(303)]
    await svc.build(1, 777)  # took off (seen)
    assert await _count(database, FlightRecord) == 0  # still in the air
    faction._members = [
        FactionMember(
            player_id=303,
            name="Enemy303",
            level=40,
            status=TornStatus(state=PlayerState.TRAVELING, description="In Japan"),
        )
    ]
    await svc.build(1, 777)  # landed

    async with database.session() as session:
        rows = (await session.scalars(select(FlightRecord))).all()
    assert len(rows) == 1
    assert rows[0].place == "Japan"
    assert rows[0].plane_type == "light_aircraft"
    assert rows[0].method == "PI"  # the plane decides, whatever the (test-instant) duration
    assert rows[0].landed_at >= rows[0].departed_at


async def test_build_does_not_record_a_flight_whose_take_off_it_missed(
    database: Database,
) -> None:
    faction = _StubFaction([_flying(304)])  # already in the air at first look
    svc = EnemyRosterService(
        database, cast(FactionService, faction), cast(FFScouterClient, _StubFFScouter({}))
    )
    await svc.build(1, 777)
    faction._members = [_member(304, level=40)]
    await svc.build(1, 777)
    assert await _count(database, FlightRecord) == 0


async def test_airliner_methods_returns_each_players_latest_airliner_ticket(
    database: Database,
) -> None:
    async with database.session() as session:
        a = Player(torn_player_id=1, name="A")
        b = Player(torn_player_id=2, name="B")
        session.add_all([a, b])
        await session.flush()
        repo = FlightRecordRepository(session)
        for pid, method, hour in [(a.id, "Airline", 1), (a.id, "BCT", 2), (b.id, "PI", 1)]:
            repo.add(
                FlightRecord(
                    player_id=pid,
                    place="Japan",
                    departed_at=_T,
                    landed_at=_T + timedelta(hours=hour),
                    method=method,
                )
            )
        await session.flush()
        got = await repo.airliner_methods([a.id, b.id])
    assert got == {a.id: "BCT"}  # B only flies PI: no airliner habit to report


async def test_status_history_is_only_written_when_the_status_changes(
    database: Database,
) -> None:
    release = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)

    def hosp(description: str, until: datetime) -> FactionMember:
        return FactionMember(
            player_id=301,
            name="Enemy301",
            level=40,
            status=TornStatus(state=PlayerState.HOSPITAL, description=description, until=until),
        )

    stub = _StubFaction([hosp("In hospital for 2 hrs 0 mins", release)])
    svc = EnemyRosterService(
        database, cast(FactionService, stub), cast(FFScouterClient, _StubFFScouter(None))
    )
    await svc.build(1, 777, fetch_estimates=False)
    # Only the countdown text moved: same release time, nothing new to store.
    stub._members = [hosp("In hospital for 1 hrs 59 mins", release)]
    await svc.build(1, 777, fetch_estimates=False)
    assert await _count(database, PlayerStatusHistory) == 1
    # Meds cut the release time: that is a change and is stored.
    stub._members = [hosp("In hospital for 49 mins", release - timedelta(minutes=70))]
    await svc.build(1, 777, fetch_estimates=False)
    assert await _count(database, PlayerStatusHistory) == 2
    stub._members = [_member(301, level=40)]
    await svc.build(1, 777, fetch_estimates=False)
    assert await _count(database, PlayerStatusHistory) == 3
