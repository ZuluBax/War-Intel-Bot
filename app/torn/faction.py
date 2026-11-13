"""Faction data access — our faction and enemy factions.

Backed by Torn API v2 (contract verified live — see docs/api-contracts.md):

* ``GET /v2/faction/{id}/basic``   -> ``{"basic": {id, name, tag, respect, ...}}``
* ``GET /v2/faction/{id}/members`` -> ``{"members": [ {id, name, level,
  days_in_faction, position, last_action{timestamp}, status{state,...}} ]}``

Both work for our own faction *and* for an arbitrary enemy faction id with a
Limited-access faction key. Missing fields map to ``None`` — never invented.
"""

from __future__ import annotations

import time
from typing import Any

from app.torn.client import TornClient
from app.torn.models import FactionMember, FactionSummary, TornStatus, parse_player_state
from app.utils.time import from_unix


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _parse_status(raw: Any) -> TornStatus:
    if not isinstance(raw, dict):
        return TornStatus()
    until_raw = raw.get("until")
    plane_raw = raw.get("plane_image_type")
    details_raw = raw.get("details")
    return TornStatus(
        state=parse_player_state(raw.get("state") or raw.get("description")),
        description=raw.get("description") or raw.get("state"),
        details=details_raw if isinstance(details_raw, str) and details_raw else None,
        until=from_unix(until_raw) if isinstance(until_raw, (int, float)) and until_raw else None,
        plane_type=plane_raw if isinstance(plane_raw, str) and plane_raw else None,
    )


def _parse_member(raw: dict[str, Any]) -> FactionMember | None:
    player_id = _as_int(raw.get("id"))
    if player_id is None:
        return None
    last_action = raw.get("last_action")
    ts = last_action.get("timestamp") if isinstance(last_action, dict) else None
    online = last_action.get("status") if isinstance(last_action, dict) else None
    return FactionMember(
        player_id=player_id,
        name=raw.get("name"),
        level=_as_int(raw.get("level")),
        position=raw.get("position"),
        days_in_faction=_as_int(raw.get("days_in_faction")),
        last_action=from_unix(ts) if isinstance(ts, (int, float)) and ts else None,
        online_status=online if isinstance(online, str) and online else None,
        status=_parse_status(raw.get("status")),
    )


class FactionService:
    def __init__(self, client: TornClient) -> None:
        self._client = client

    async def get_basic(self, faction_id: int) -> FactionSummary:
        body = await self._client.get(f"/faction/{faction_id}/basic")
        basic = body.get("basic") if isinstance(body, dict) else None
        name = basic.get("name") if isinstance(basic, dict) else None
        return FactionSummary(faction_id=faction_id, name=name)

    async def get_members(self, faction_id: int) -> FactionSummary:
        # Torn caches every answer for up to 30 s unless the request is unique; its API
        # page says bypassing that with a ``timestamp`` parameter is allowed. Without it,
        # a 15 s status refresh would get the same stale statuses twice in a row.
        body = await self._client.get(
            f"/faction/{faction_id}/members", params={"timestamp": int(time.time())}
        )
        raw_members = body.get("members") if isinstance(body, dict) else None

        rows: list[dict[str, Any]] = []
        if isinstance(raw_members, list):
            rows = [m for m in raw_members if isinstance(m, dict)]
        elif isinstance(raw_members, dict):
            # Defensive: older/v1-style responses key members by id.
            rows = [v for v in raw_members.values() if isinstance(v, dict)]

        members = tuple(m for m in (_parse_member(r) for r in rows) if m is not None)
        return FactionSummary(faction_id=faction_id, members=members)

    async def get_faction(self, faction_id: int, *, with_members: bool = True) -> FactionSummary:
        """Return a faction's name and (optionally) its full member roster."""
        basic = await self.get_basic(faction_id)
        if not with_members:
            return basic
        roster = await self.get_members(faction_id)
        return FactionSummary(faction_id=faction_id, name=basic.name, members=roster.members)
