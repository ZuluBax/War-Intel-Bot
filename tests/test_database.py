from __future__ import annotations

import pytest

from app.database.database import Database
from app.database.models import Faction, StatEstimate, War
from app.database.repositories import (
    FactionRepository,
    PlayerRepository,
    StatEstimateRepository,
    WarRepository,
)

# The `database` fixture lives in tests/conftest.py.


async def test_schema_creates_and_faction_upserts(database: Database) -> None:
    async with database.session() as session:
        repo = FactionRepository(session)
        first = await repo.upsert(12345, name="Us", is_ours=True)
        assert first.id is not None
    async with database.session() as session:
        repo = FactionRepository(session)
        again = await repo.upsert(12345, name="Us Renamed", is_ours=False)
        assert again.name == "Us Renamed"
        assert again.is_ours is True  # is_ours is sticky


async def test_war_torn_id_is_unique(database: Database) -> None:
    async with database.session() as session:
        f = FactionRepository(session)
        ours = await f.upsert(1, name="Us", is_ours=True)
        them = await f.upsert(2, name="Them", is_ours=False)
        WarRepository(session).add(
            War(
                guild_id=42,
                torn_war_id=555,
                our_faction_id=ours.id,
                enemy_faction_id=them.id,
                status="WAR_ACTIVE",
            )
        )
    async with database.session() as session:
        existing = await WarRepository(session).get_by_torn_war_id(42, 555)
        assert existing is not None


async def test_server_default_timestamps_round_trip_timezone_aware(database: Database) -> None:
    """Regression: SQLite has no native tz-aware datetime type, so aiosqlite
    hands back naive datetimes for any ``DateTime(timezone=True)`` column
    unless it's wrapped in ``UTCDateTime`` (app.database.models) — this broke
    ``war_log_event_embed`` the moment it tried to format a real DB-sourced
    ``occurred_at``. Every timestamp in the app must stay tz-aware (spec:
    app.utils.time)."""
    async with database.session() as session:
        faction = await FactionRepository(session).upsert(1, name="Us", is_ours=True)
        fid = faction.id
    async with database.session() as session:
        row = await session.get(Faction, fid)
        assert row is not None
        assert row.created_at.tzinfo is not None
        assert row.updated_at.tzinfo is not None


async def test_stat_estimates_are_append_only_history(database: Database) -> None:
    async with database.session() as session:
        player = await PlayerRepository(session).upsert(42, name="Enemy", level=50)
        pid = player.id
    async with database.session() as session:
        repo = StatEstimateRepository(session)
        repo.add(StatEstimate(player_id=pid, estimated_bs=2.81e9, source="ffscouter"))
    async with database.session() as session:
        repo = StatEstimateRepository(session)
        repo.add(StatEstimate(player_id=pid, estimated_bs=2.84e9, source="ffscouter"))
    async with database.session() as session:
        repo = StatEstimateRepository(session)
        history = await repo.history_for_player(pid)
        latest = await repo.latest_for_player(pid)
        assert len(history) == 2
        assert latest is not None and latest.estimated_bs == pytest.approx(2.84e9)
