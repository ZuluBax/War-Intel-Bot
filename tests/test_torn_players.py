from __future__ import annotations

from typing import cast

from app.torn.client import TornClient
from app.torn.errors import TornAPIError
from app.torn.models import PlayerState
from app.torn.players import PlayerService
from tests.conftest import FakeTornClient
from tests.torn_fixtures import USER_PROFILE


def _service(routes: dict[str, object]) -> PlayerService:
    return PlayerService(cast(TornClient, FakeTornClient(routes)))


async def test_get_profile_parses_real_shape() -> None:
    svc = _service({r"/user/60123": USER_PROFILE})
    profile = await svc.get_profile(60123)
    assert profile.name == "EnemyGuy"
    assert profile.level == 44
    assert profile.faction_id == 60001
    assert profile.status.state is PlayerState.OKAY


async def test_get_profiles_skips_failing_ids() -> None:
    def route(path: str) -> dict[str, object]:
        if "/user/999" in path:
            raise TornAPIError("Torn API error 6: Incorrect ID")
        return USER_PROFILE

    svc = _service({r"/user/\d+": route})
    result = await svc.get_profiles([60123, 999])
    assert set(result) == {60123}


async def test_get_profile_handles_missing_profile_key() -> None:
    svc = _service({r"/user/1": {"error": None}})
    profile = await svc.get_profile(1)
    assert profile.player_id == 1
    assert profile.level is None
    assert profile.status.state is PlayerState.UNKNOWN
