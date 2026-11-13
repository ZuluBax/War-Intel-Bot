from __future__ import annotations

from sqlalchemy import select

from app.database.database import Database
from app.database.models import TargetRanking
from app.database.repositories import PlayerRepository
from app.torn.models import PlayerState
from app.war.roster import Roster, RosterEntry
from app.war.target_service import TargetingService
from app.war.targeting import TargetingWeights

WAR_ID = 1


def _roster(*entries: RosterEntry) -> Roster:
    return Roster(war_id=WAR_ID, enemy_faction_id=777, entries=list(entries))


async def _seed_player(database: Database, torn_id: int, *, name: str, level: int = 20) -> None:
    async with database.session() as session:
        await PlayerRepository(session).upsert(torn_id, name=name, level=level)


async def test_refresh_persists_a_ranking_row_per_confirmed_player(database: Database) -> None:
    await _seed_player(database, 101, name="Weak")
    await _seed_player(database, 102, name="Strong")
    roster = _roster(
        RosterEntry(player_id=101, name="Weak", level=20, estimated_bs=1.0e9, fair_fight=1.0,
                    state=PlayerState.OKAY),
        RosterEntry(player_id=102, name="Strong", level=20, estimated_bs=9.0e9, fair_fight=1.0,
                    state=PlayerState.OKAY),
    )
    svc = TargetingService(database)

    views = await svc.refresh(WAR_ID, roster)

    assert [v.player_id for v in views] == [101, 102]
    assert views[0].rank == 1 and views[1].rank == 2

    async with database.session() as session:
        rows = list(await session.scalars(select(TargetRanking).where(TargetRanking.war_id == WAR_ID)))
    assert len(rows) == 2
    assert {r.rank for r in rows} == {1, 2}


async def test_refresh_replaces_the_snapshot_rather_than_appending(database: Database) -> None:
    await _seed_player(database, 201, name="A")
    await _seed_player(database, 202, name="B")
    roster = _roster(
        RosterEntry(player_id=201, name="A", level=20, estimated_bs=1.0e9, state=PlayerState.OKAY),
        RosterEntry(player_id=202, name="B", level=20, estimated_bs=9.0e9, state=PlayerState.OKAY),
    )
    svc = TargetingService(database)

    await svc.refresh(WAR_ID, roster)
    await svc.refresh(WAR_ID, roster)  # a second refresh must not accumulate rows

    async with database.session() as session:
        rows = list(await session.scalars(select(TargetRanking).where(TargetRanking.war_id == WAR_ID)))
    assert len(rows) == 2


async def test_hospitalised_player_is_capped_at_low_and_ranked_last(database: Database) -> None:
    await _seed_player(database, 301, name="Weakest")
    await _seed_player(database, 302, name="Mediocre")
    roster = _roster(
        RosterEntry(player_id=301, name="Weakest", level=20, estimated_bs=1.0e8,
                    state=PlayerState.HOSPITAL),
        RosterEntry(player_id=302, name="Mediocre", level=20, estimated_bs=5.0e9,
                    state=PlayerState.OKAY),
    )
    views = await TargetingService(database).refresh(WAR_ID, roster)

    by_id = {v.player_id: v for v in views}
    assert by_id[301].priority == "LOW"
    assert by_id[301].is_available is False
    assert [v.player_id for v in views] == [302, 301]  # available target ranked first


async def test_unknown_player_not_in_the_db_is_still_returned_but_not_persisted(
    database: Database,
) -> None:
    # No PlayerRepository.upsert() call for 401 -> it's on the roster but unknown to the DB.
    roster = _roster(
        RosterEntry(player_id=401, name="Ghost", level=20, estimated_bs=1.0e9, state=PlayerState.OKAY)
    )
    views = await TargetingService(database).refresh(WAR_ID, roster)

    assert len(views) == 1
    assert views[0].player_id == 401

    async with database.session() as session:
        rows = list(await session.scalars(select(TargetRanking).where(TargetRanking.war_id == WAR_ID)))
    assert rows == []


async def test_refresh_carries_estimated_bs_and_fair_fight_onto_the_view(
    database: Database,
) -> None:
    """Regression test: TargetView used to drop estimated_bs/fair_fight even
    though the roster entry it's built from already has them."""
    await _seed_player(database, 601, name="Solo")
    roster = _roster(
        RosterEntry(
            player_id=601, name="Solo", level=20, estimated_bs=3.0e9, fair_fight=1.25,
            state=PlayerState.OKAY,
        )
    )
    views = await TargetingService(database).refresh(WAR_ID, roster)
    assert views[0].estimated_bs == 3.0e9
    assert views[0].fair_fight == 1.25


async def test_custom_weights_are_forwarded_to_the_algorithm(database: Database) -> None:
    await _seed_player(database, 501, name="LowFF")
    await _seed_player(database, 502, name="HighFF")
    roster = _roster(
        RosterEntry(player_id=501, name="LowFF", level=20, estimated_bs=1.0e9, fair_fight=3.0,
                    state=PlayerState.OKAY),
        RosterEntry(player_id=502, name="HighFF", level=20, estimated_bs=9.0e9, fair_fight=1.0,
                    state=PlayerState.OKAY),
    )
    ff_only = TargetingWeights(strength_weight=0.0, fair_fight_weight=1.0, level_weight=0.0)
    views = await TargetingService(database, weights=ff_only).refresh(WAR_ID, roster)
    assert views[0].player_id == 502
