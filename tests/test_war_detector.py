from __future__ import annotations

from datetime import timedelta

from app.torn.models import RankedWar, RankedWarFaction
from app.utils.time import utcnow
from app.war.detector import WarDetector, WarState, assess

OUR_ID = 12345
ENEMY_ID = 67890


def _war(**overrides: object) -> RankedWar:
    now = utcnow()
    defaults = dict(
        war_id=1,
        start=now - timedelta(hours=1),
        end=None,
        target_score=1000,
        factions=(
            RankedWarFaction(OUR_ID, "Us", 10),
            RankedWarFaction(ENEMY_ID, "Them", 5),
        ),
        winner_faction_id=None,
    )
    defaults.update(overrides)
    return RankedWar(**defaults)  # type: ignore[arg-type]


def test_no_war_when_none() -> None:
    assert assess(None, our_faction_id=OUR_ID).state is WarState.NO_WAR


def test_no_war_when_we_are_not_a_participant() -> None:
    war = _war(factions=(RankedWarFaction(1, "A"), RankedWarFaction(2, "B")))
    assert assess(war, our_faction_id=OUR_ID).state is WarState.NO_WAR


def test_detected_when_start_in_future() -> None:
    war = _war(start=utcnow() + timedelta(hours=2))
    result = assess(war, our_faction_id=OUR_ID)
    assert result.state is WarState.WAR_DETECTED
    assert result.enemy_faction_id == ENEMY_ID


def test_active_when_between_start_and_end() -> None:
    war = _war(start=utcnow() - timedelta(hours=1), end=utcnow() + timedelta(hours=1))
    assert assess(war, our_faction_id=OUR_ID).state is WarState.WAR_ACTIVE


def test_active_when_end_unknown() -> None:
    assert assess(_war(end=None), our_faction_id=OUR_ID).state is WarState.WAR_ACTIVE


def test_finished_when_end_in_past() -> None:
    war = _war(start=utcnow() - timedelta(days=1), end=utcnow() - timedelta(hours=1))
    assert assess(war, our_faction_id=OUR_ID).state is WarState.WAR_FINISHED


def test_finished_when_winner_declared_even_if_end_missing() -> None:
    war = _war(end=None, winner_faction_id=OUR_ID)
    assert assess(war, our_faction_id=OUR_ID).state is WarState.WAR_FINISHED


class _StubWarfare:
    def __init__(self, war: RankedWar | None) -> None:
        self._war = war
        self.asked_for: int | None = None

    async def get_current_ranked_war(self, faction_id: int) -> RankedWar | None:
        self.asked_for = faction_id
        return self._war

    async def list_ranked_wars(self, faction_id: int) -> list[RankedWar]:
        self.asked_for = faction_id
        return [self._war] if self._war else []


async def test_detector_reports_no_war_when_service_returns_none() -> None:
    stub = _StubWarfare(None)
    detector = WarDetector(stub, our_faction_id=OUR_ID)  # type: ignore[arg-type]
    result = await detector.detect()
    assert result.state is WarState.NO_WAR
    assert stub.asked_for == OUR_ID


async def test_detector_classifies_active_war_and_names_enemy() -> None:
    detector = WarDetector(_StubWarfare(_war()), our_faction_id=OUR_ID)  # type: ignore[arg-type]
    result = await detector.detect()
    assert result.state is WarState.WAR_ACTIVE
    assert result.enemy_faction_id == ENEMY_ID


async def test_detector_lists_every_ranked_war_for_our_faction() -> None:
    stub = _StubWarfare(_war())
    detector = WarDetector(stub, our_faction_id=OUR_ID)  # type: ignore[arg-type]
    wars = await detector.ranked_wars()
    assert [w.war_id for w in wars] == [_war().war_id]
    assert stub.asked_for == OUR_ID
