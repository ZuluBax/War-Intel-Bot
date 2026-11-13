from __future__ import annotations

from typing import cast

from app.torn.client import TornClient
from app.torn.warfare import WarfareService
from tests.conftest import FakeTornClient
from tests.torn_fixtures import (
    FACTION_RANKEDWARS_ACTIVE,
    FACTION_RANKEDWARS_FINISHED,
    FACTION_WARS_NO_RANKED,
)


def _service(routes: dict[str, object]) -> WarfareService:
    return WarfareService(cast(TornClient, FakeTornClient(routes)))


async def test_list_ranked_wars_parses_finished_history() -> None:
    svc = _service({r"/faction/53228/rankedwars": FACTION_RANKEDWARS_FINISHED})
    wars = await svc.list_ranked_wars(53228)
    assert [w.war_id for w in wars] == [48328, 47882]
    first = wars[0]
    assert first.end is not None
    assert first.winner_faction_id == 49384
    assert {f.faction_id for f in first.factions} == {49384, 53228}
    assert first.opponent_of(53228).faction_id == 49384


async def test_get_current_ranked_war_returns_newest_for_our_faction() -> None:
    svc = _service({r"/faction/53228/rankedwars": FACTION_RANKEDWARS_ACTIVE})
    war = await svc.get_current_ranked_war(53228)
    assert war is not None
    assert war.war_id == 49999
    assert war.end is None
    assert war.winner_faction_id is None
    assert war.opponent_of(53228).faction_id == 60001


async def test_get_current_ranked_war_none_when_history_empty() -> None:
    svc = _service({r"/faction/53228/rankedwars": {"rankedwars": []}})
    assert await svc.get_current_ranked_war(53228) is None


async def test_has_live_ranked_war_reads_wars_ranked_slot() -> None:
    svc = _service({r"/faction/53228/wars": FACTION_WARS_NO_RANKED})
    assert await svc.has_live_ranked_war(53228) is False


async def test_has_live_ranked_war_none_when_field_absent() -> None:
    svc = _service({r"/faction/53228/wars": {"wars": {"raids": []}}})
    assert await svc.has_live_ranked_war(53228) is None
