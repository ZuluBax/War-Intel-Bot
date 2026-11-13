from __future__ import annotations

from datetime import timedelta

from app.database.database import Database
from app.database.models import Faction, War
from app.utils.time import utcnow
from app.war.history import WarHistoryService

GUILD_ID = 1


async def _seed_finished_war(
    database: Database,
    *,
    torn_war_id: int,
    enemy_name: str,
    result: str,
    ended: int,  # days ago
) -> None:
    async with database.session() as session:
        us = Faction(torn_faction_id=torn_war_id * 10 + 1, name="Us", is_ours=True)
        them = Faction(torn_faction_id=torn_war_id * 10 + 2, name=enemy_name, is_ours=False)
        session.add_all([us, them])
        await session.flush()
        session.add(
            War(
                guild_id=GUILD_ID,
                torn_war_id=torn_war_id,
                our_faction_id=us.id,
                our_faction_name="Us",
                enemy_faction_id=them.id,
                enemy_faction_name=enemy_name,
                status="WAR_FINISHED",
                result=result,
                war_end=utcnow() - timedelta(days=ended),
            )
        )


async def _seed_active_war(database: Database, *, torn_war_id: int) -> None:
    async with database.session() as session:
        us = Faction(torn_faction_id=torn_war_id * 10 + 1, name="Us", is_ours=True)
        them = Faction(torn_faction_id=torn_war_id * 10 + 2, name="Ongoing Enemy", is_ours=False)
        session.add_all([us, them])
        await session.flush()
        session.add(
            War(
                guild_id=GUILD_ID,
                torn_war_id=torn_war_id,
                our_faction_id=us.id,
                our_faction_name="Us",
                enemy_faction_id=them.id,
                enemy_faction_name="Ongoing Enemy",
                status="WAR_ACTIVE",
            )
        )


async def test_recent_returns_only_finished_wars_newest_first(database: Database) -> None:
    await _seed_finished_war(database, torn_war_id=1, enemy_name="Old Foe", result="WIN", ended=10)
    await _seed_finished_war(database, torn_war_id=2, enemy_name="Recent Foe", result="LOSS", ended=1)
    await _seed_active_war(database, torn_war_id=3)

    wars = await WarHistoryService(database).recent(GUILD_ID)

    assert [w.enemy_faction_name for w in wars] == ["Recent Foe", "Old Foe"]
    assert [w.result for w in wars] == ["LOSS", "WIN"]


async def test_recent_is_guild_scoped(database: Database) -> None:
    await _seed_finished_war(database, torn_war_id=1, enemy_name="Their Foe", result="WIN", ended=1)

    other_guild_wars = await WarHistoryService(database).recent(GUILD_ID + 1)
    assert other_guild_wars == []


async def test_recent_respects_limit(database: Database) -> None:
    for i in range(5):
        await _seed_finished_war(
            database, torn_war_id=100 + i, enemy_name=f"Foe{i}", result="WIN", ended=i
        )
    wars = await WarHistoryService(database).recent(GUILD_ID, limit=2)
    assert len(wars) == 2


async def test_recent_with_no_history_is_empty(database: Database) -> None:
    assert await WarHistoryService(database).recent(GUILD_ID) == []
