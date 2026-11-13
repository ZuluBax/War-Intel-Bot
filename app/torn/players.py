"""Player data access — public profile fields and live status.

Backed by ``GET /v2/user/{id}?selections=profile`` (contract verified live):
``{"profile": {id, name, level, faction_id, status{state, description, until},
last_action{status, timestamp}}}``. Works for any player id with a
Limited-access key.

Torn has no bulk user endpoint, so :meth:`get_profiles` fans out one request per
player under the shared rate limiter. Callers (Phase 5 refresh) decide how many
of a roster to refresh per cycle within budget.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.torn.client import TornClient
from app.torn.errors import TornAPIError
from app.torn.models import TornStatus, parse_player_state
from app.utils.logging import get_logger
from app.utils.time import from_unix

logger = get_logger(__name__)


@dataclass(frozen=True)
class PlayerProfile:
    player_id: int
    name: str | None = None
    level: int | None = None
    faction_id: int | None = None
    status: TornStatus = field(default_factory=TornStatus)


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
    return TornStatus(
        state=parse_player_state(raw.get("state") or raw.get("description")),
        description=raw.get("description") or raw.get("state"),
        until=from_unix(until_raw) if isinstance(until_raw, (int, float)) and until_raw else None,
    )


class PlayerService:
    def __init__(self, client: TornClient) -> None:
        self._client = client

    async def get_profile(self, player_id: int) -> PlayerProfile:
        body = await self._client.get(f"/user/{player_id}?selections=profile")
        profile = body.get("profile") if isinstance(body, dict) else None
        if not isinstance(profile, dict):
            return PlayerProfile(player_id=player_id)
        return PlayerProfile(
            player_id=player_id,
            name=profile.get("name"),
            level=_as_int(profile.get("level")),
            faction_id=_as_int(profile.get("faction_id")),
            status=_parse_status(profile.get("status")),
        )

    async def get_status(self, player_id: int) -> TornStatus:
        return (await self.get_profile(player_id)).status

    async def get_profiles(self, player_ids: Iterable[int]) -> dict[int, PlayerProfile]:
        """Best-effort profile for many players. A failure for one player is
        logged and that player is simply omitted — one bad id never sinks the
        whole refresh."""
        ids = sorted({int(p) for p in player_ids})
        out: dict[int, PlayerProfile] = {}
        for pid in ids:
            try:
                out[pid] = await self.get_profile(pid)
            except TornAPIError as exc:
                logger.warning("torn.player_profile_failed", player_id=pid, detail=str(exc))
        return out

    async def get_statuses(self, player_ids: Iterable[int]) -> dict[int, TornStatus]:
        profiles = await self.get_profiles(player_ids)
        return {pid: p.status for pid, p in profiles.items()}
