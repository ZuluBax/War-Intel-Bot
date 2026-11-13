"""The reused war category takes the new enemy's name the moment a war is confirmed."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from app.database.database import Database
from app.discord.gateway import GuildGateway
from app.discord.war_channels import CHANNEL_LAYOUT, WarBoard, WarChannelManager, WarLogBoard
from app.discord.war_publish import DiscordWarPublisher
from app.torn.models import RankedWar, RankedWarFaction
from app.utils.time import utcnow
from app.war.detector import WarState
from tests.fake_gateway import FakeGuildGateway
from tests.test_war_manager import OUR_ID, _assessment, _manager, _StubDetector, _StubRoster, _war


def _publisher(database: Database, fake: FakeGuildGateway) -> DiscordWarPublisher:
    gateway = cast(GuildGateway, fake)
    return DiscordWarPublisher(
        database,
        WarChannelManager(database, gateway),
        WarBoard(database, gateway),
        WarLogBoard(database, gateway),
    )


def _next_war(*, start_in: timedelta) -> RankedWar:
    return RankedWar(
        war_id=50000,
        start=utcnow() + start_in,
        end=None,
        target_score=5000,
        factions=(
            RankedWarFaction(OUR_ID, "Null & Void", 0),
            RankedWarFaction(60002, "Brand New Foe", 0),
        ),
    )


async def test_category_is_renamed_as_soon_as_the_next_war_is_confirmed(
    database: Database,
) -> None:
    fake = FakeGuildGateway()
    detector = _StubDetector(_assessment(WarState.WAR_ACTIVE, _war()))  # vs "Rival Crew"
    mgr = _manager(
        database, detector, roster_service=_StubRoster(), publisher=_publisher(database, fake)
    )

    await mgr.tick()
    [category_id] = fake.categories
    assert "Rival Crew" in fake.categories[category_id]

    detector.assessment = _assessment(WarState.WAR_FINISHED, _war(end=utcnow(), winner=OUR_ID))
    await mgr.tick()
    assert "Rival Crew" in fake.categories[category_id]  # nothing changes just because it ended

    # Torn now lists the next ranked war — matched, but it has not started yet.
    detector.assessment = _assessment(WarState.WAR_DETECTED, _next_war(start_in=timedelta(days=1)))
    await mgr.tick()

    assert list(fake.categories) == [category_id]  # the same category, not a new one
    assert "Brand New Foe" in fake.categories[category_id]
    assert "Rival Crew" not in fake.categories[category_id]
    assert len(fake.channels) == len(CHANNEL_LAYOUT)  # and the same channels
