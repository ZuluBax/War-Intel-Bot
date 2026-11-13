from __future__ import annotations

from typing import cast

import pytest

from app.torn.client import TornClient
from app.torn.faction import FactionService
from app.torn.models import PlayerState
from tests.conftest import FakeTornClient
from tests.torn_fixtures import FACTION_BASIC, FACTION_MEMBERS


def _service(routes: dict[str, object]) -> FactionService:
    return FactionService(cast(TornClient, FakeTornClient(routes)))


async def test_get_basic_returns_name() -> None:
    svc = _service({r"/faction/10001/basic": FACTION_BASIC})
    summary = await svc.get_basic(10001)
    assert summary.faction_id == 10001
    assert summary.name == "Ours & Co"


async def test_get_members_parses_real_shape() -> None:
    svc = _service({r"/faction/20001/members": FACTION_MEMBERS})
    summary = await svc.get_members(20001)
    assert [m.player_id for m in summary.members] == [1000011, 1000012, 1000013]

    katz, xapd, nice = summary.members
    assert katz.level == 20
    assert katz.status.state is PlayerState.OKAY
    assert katz.last_action is not None
    assert xapd.status.state is PlayerState.HOSPITAL
    assert xapd.status.until is not None
    assert nice.status.state is PlayerState.TRAVELING
    assert nice.days_in_faction == 273


async def test_get_faction_combines_basic_and_members() -> None:
    svc = _service(
        {
            r"/faction/10001/basic": FACTION_BASIC,
            r"/faction/10001/members": FACTION_MEMBERS,
        }
    )
    summary = await svc.get_faction(10001)
    assert summary.name == "Ours & Co"
    assert len(summary.members) == 3


async def test_get_members_tolerates_dict_keyed_members() -> None:
    svc = _service({r"/faction/1/members": {"members": {"1000011": FACTION_MEMBERS["members"][0]}}})
    summary = await svc.get_members(1)
    assert len(summary.members) == 1


async def test_get_members_skips_rows_without_id() -> None:
    svc = _service({r"/faction/1/members": {"members": [{"name": "no id"}, FACTION_MEMBERS["members"][0]]}})
    summary = await svc.get_members(1)
    assert [m.player_id for m in summary.members] == [1000011]


async def test_empty_torn_key_raises() -> None:
    with pytest.raises(ValueError, match=r"[Kk]ey"):
        TornClient(api_key="")


async def test_get_members_reads_the_plane_type_of_a_flying_member() -> None:
    """Torn sends ``plane_image_type`` only while flying; the landing estimate needs it."""
    flying = {
        "id": 5,
        "name": "Flyer",
        "level": 30,
        "status": {
            "description": "Traveling from Torn to Japan",
            "details": None,
            "state": "Traveling",
            "color": "blue",
            "until": None,
            "plane_image_type": "light_aircraft",
        },
    }
    grounded = {
        "id": 6,
        "name": "Grounded",
        "level": 30,
        "status": {"description": "Okay", "state": "Okay", "color": "green", "until": None},
    }
    summary = await _service({r"/faction/1/members": {"members": [flying, grounded]}}).get_members(1)
    assert summary.members[0].status.plane_type == "light_aircraft"
    assert summary.members[0].status.until is None  # Torn gives no arrival time for a flight
    assert summary.members[1].status.plane_type is None


async def test_get_members_reads_torns_online_label() -> None:
    def member(pid: int, status: str | None) -> dict[str, object]:
        last = {"status": status, "timestamp": 1_789_900_000, "relative": "1 minute ago"}
        return {"id": pid, "name": f"P{pid}", "level": 10, "last_action": last if status else None,
                "status": {"description": "Okay", "state": "Okay", "until": None}}

    summary = await _service(
        {r"/faction/1/members": {"members": [member(1, "Online"), member(2, "Idle"),
                                             member(3, "Offline"), member(4, None)]}}
    ).get_members(1)
    assert [m.online_status for m in summary.members] == ["Online", "Idle", "Offline", None]


async def test_get_members_asks_torn_for_an_uncached_answer() -> None:
    # Torn caches answers up to 30 s unless the request is unique (timestamp param).
    seen: list[dict[str, object] | None] = []

    class _Recording(FakeTornClient):
        async def get(self, path: str, params: dict[str, object] | None = None) -> dict:  # type: ignore[override]
            seen.append(params)
            return await super().get(path, params)

    svc = FactionService(cast(TornClient, _Recording({r"/members": FACTION_MEMBERS})))
    await svc.get_members(20001)
    assert seen and seen[0] is not None and isinstance(seen[0].get("timestamp"), int)
