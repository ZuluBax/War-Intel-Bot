"""``create_all`` adds new nullable columns to tables that already exist (no Alembic yet)."""

from __future__ import annotations

from app.database.database import Database


async def _columns(db: Database, table: str) -> set[str]:
    async with db.engine.connect() as conn:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        return {row[1] for row in result}


async def test_create_all_adds_columns_an_older_table_is_missing(database: Database) -> None:
    async with database.engine.begin() as conn:  # make war_hits look like the old version
        await conn.exec_driver_sql("ALTER TABLE war_hits DROP COLUMN chain")
        await conn.exec_driver_sql("ALTER TABLE war_hits DROP COLUMN warlord")
    assert not {"chain", "warlord"} & await _columns(database, "war_hits")

    await database.create_all()

    assert {"chain", "warlord"} <= await _columns(database, "war_hits")


async def test_create_all_is_a_no_op_on_an_up_to_date_schema(database: Database) -> None:
    before = await _columns(database, "war_hits")
    await database.create_all()
    await database.create_all()
    assert await _columns(database, "war_hits") == before


async def test_existing_rows_survive_the_column_being_added(database: Database) -> None:
    async with database.engine.begin() as conn:
        await conn.exec_driver_sql("INSERT INTO factions (id, torn_faction_id, is_ours, created_at, updated_at) "
                                   "VALUES (1, 1, 1, '2026-01-01', '2026-01-01')")
        await conn.exec_driver_sql(
            "INSERT INTO wars (id, guild_id, our_faction_id, enemy_faction_id, status, created_at, updated_at) "
            "VALUES (1, 1, 1, 1, 'WAR_ACTIVE', '2026-01-01', '2026-01-01')")
        await conn.exec_driver_sql("ALTER TABLE war_hits DROP COLUMN chain")
        await conn.exec_driver_sql("ALTER TABLE war_hits DROP COLUMN warlord")
        await conn.exec_driver_sql(
            "INSERT INTO war_hits (war_id, torn_attack_id, started_at, result, is_war_hit, is_outgoing) "
            "VALUES (1, 99, '2026-01-01', 'Attacked', 1, 1)")

    await database.create_all()

    async with database.engine.connect() as conn:
        row = (await conn.exec_driver_sql(
            "SELECT torn_attack_id, chain, warlord FROM war_hits")).one()
    assert tuple(row) == (99, None, None)  # kept, new columns empty (to be backfilled)
