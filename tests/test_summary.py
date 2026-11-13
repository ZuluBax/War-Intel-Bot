from __future__ import annotations

from datetime import UTC, datetime

from app.database.database import Database
from app.database.models import (
    Faction,
    PlayerStatusHistory,
    StatEstimate,
    TargetRanking,
    War,
    WarPlayer,
)
from app.database.repositories import PlayerRepository
from app.torn.models import PlayerState
from app.war.summary import load_target_view

WAR_ID = 1


async def _seed_war_with_one_ranked_target(database: Database) -> None:
    async with database.session() as session:
        us = Faction(torn_faction_id=1, name="Us", is_ours=True)
        them = Faction(torn_faction_id=2, name="Them", is_ours=False)
        session.add_all([us, them])
        await session.flush()
        war = War(
            id=WAR_ID,
            guild_id=1,
            torn_war_id=1,
            our_faction_id=us.id,
            enemy_faction_id=them.id,
            status="WAR_ACTIVE",
        )
        session.add(war)

        player = await PlayerRepository(session).upsert(9001, name="Target", level=30)
        session.add(
            StatEstimate(player_id=player.id, estimated_bs=2_500_000_000.0, fair_fight=1.75)
        )
        session.add(
            TargetRanking(war_id=WAR_ID, player_id=player.id, rank=1, priority="HIGH", score=0.9)
        )
        await session.flush()


async def test_load_target_view_carries_the_latest_battle_stat_estimate(
    database: Database,
) -> None:
    """Regression test: /targets used to omit estimated_bs/fair_fight entirely
    because load_target_view() never looked them up."""
    await _seed_war_with_one_ranked_target(database)

    async with database.session() as session:
        war = await session.get(War, WAR_ID)
        assert war is not None
        views = await load_target_view(session, war)

    assert len(views) == 1
    assert views[0].estimated_bs == 2_500_000_000.0
    assert views[0].fair_fight == 1.75


async def test_load_target_view_carries_the_full_status_for_the_live_board(
    database: Database,
) -> None:
    """/targets shares the live board, so it needs the status text, release time, plane
    and take-off time — not just the OKAY/HOSPITAL/... state."""
    await _seed_war_with_one_ranked_target(database)
    release = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    took_off = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with database.session() as session:
        player = await PlayerRepository(session).get_by_torn_id(9001)
        assert player is not None
        session.add(
            PlayerStatusHistory(
                player_id=player.id,
                war_id=WAR_ID,
                state="TRAVELING",
                description="Traveling from Torn to Japan",
                until=release,
                plane_type="light_aircraft",
            )
        )
        session.add(
            WarPlayer(
                war_id=WAR_ID,
                player_id=player.id,
                status_description="Traveling from Torn to Japan",
                status_since=took_off,
            )
        )

    async with database.session() as session:
        war = await session.get(War, WAR_ID)
        assert war is not None
        view = (await load_target_view(session, war))[0]

    assert view.state is PlayerState.TRAVELING
    assert view.status_description == "Traveling from Torn to Japan"
    assert view.status_until == release
    assert view.plane_type == "light_aircraft"
    assert view.status_since == took_off


async def test_load_target_view_ignores_a_take_off_time_from_an_older_status(
    database: Database,
) -> None:
    """The stored take-off belongs to the status the roster last saw; if the latest
    recorded status differs, using it would give a wrong landing time."""
    await _seed_war_with_one_ranked_target(database)
    async with database.session() as session:
        player = await PlayerRepository(session).get_by_torn_id(9001)
        assert player is not None
        session.add(
            PlayerStatusHistory(
                player_id=player.id, war_id=WAR_ID, state="TRAVELING", description="In Japan"
            )
        )
        session.add(
            WarPlayer(
                war_id=WAR_ID,
                player_id=player.id,
                status_description="Traveling from Torn to Japan",
                status_since=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
            )
        )

    async with database.session() as session:
        war = await session.get(War, WAR_ID)
        assert war is not None
        view = (await load_target_view(session, war))[0]

    assert view.status_since is None



async def test_load_target_view_carries_the_online_label(database: Database) -> None:
    await _seed_war_with_one_ranked_target(database)
    async with database.session() as session:
        player = await PlayerRepository(session).get_by_torn_id(9001)
        assert player is not None
        session.add(
            PlayerStatusHistory(
                player_id=player.id, war_id=WAR_ID, state="OKAY", description="Okay",
                online_status="Online",
            )
        )
    async with database.session() as session:
        war = await session.get(War, WAR_ID)
        assert war is not None
        view = (await load_target_view(session, war))[0]
    assert view.online_status == "Online"
