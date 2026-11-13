"""Ranked-war data access.

Backed by Torn API v2 (contract verified live — see docs/api-contracts.md):

* ``GET /v2/faction/{id}/rankedwars`` ->
  ``{"rankedwars": [ {id, start, end, target, winner,
     factions: [{id, name, score, chain}]} ], "_metadata": {...}}``
  Newest first. Includes the current war and full history. ``end`` / ``winner``
  are populated only once the war has finished. Timestamps are epoch seconds.

* ``GET /v2/faction/{id}/wars`` -> ``{"wars": {"ranked": null | <object>, ...}}``
  ``wars.ranked`` is a quick "is there a war right now" signal. Its non-null
  shape is not yet confirmed against a live war, so it is used only as a
  secondary confirmation — the ``/rankedwars`` list is the source of truth.

Note: ``GET /v2/torn?selections=rankedwars`` (the global list) is **v1-only**
and returns Torn error 22 — never called here.
"""

from __future__ import annotations

from typing import Any

from app.torn.client import TornClient
from app.torn.models import RankedWar, RankedWarFaction
from app.utils.logging import get_logger
from app.utils.time import from_unix

logger = get_logger(__name__)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _parse_ranked_war(raw: dict[str, Any]) -> RankedWar | None:
    war_id = _as_int(raw.get("id") or raw.get("war_id"))
    if war_id is None:
        return None

    factions_raw = raw.get("factions")
    factions: list[RankedWarFaction] = []
    if isinstance(factions_raw, list):
        iterable: list[tuple[Any, dict[str, Any]]] = [
            (f.get("id"), f) for f in factions_raw if isinstance(f, dict)
        ]
    elif isinstance(factions_raw, dict):
        iterable = [(k, v) for k, v in factions_raw.items() if isinstance(v, dict)]
    else:
        iterable = []

    for fid_hint, f in iterable:
        fid = _as_int(f.get("id") or fid_hint)
        if fid is None:
            continue
        factions.append(
            RankedWarFaction(
                faction_id=fid,
                name=f.get("name"),
                score=_as_int(f.get("score")),
                chain=_as_int(f.get("chain")),
            )
        )

    start = _as_int(raw.get("start"))
    end = _as_int(raw.get("end"))
    return RankedWar(
        war_id=war_id,
        start=from_unix(start) if start else None,
        end=from_unix(end) if end else None,
        target_score=_as_int(raw.get("target") or raw.get("target_score")),
        factions=tuple(factions),
        winner_faction_id=_as_int(raw.get("winner")),
    )


class WarfareService:
    def __init__(self, client: TornClient) -> None:
        self._client = client

    async def list_ranked_wars(self, faction_id: int) -> list[RankedWar]:
        """All ranked wars for a faction, newest first (current + history)."""
        body = await self._client.get(f"/faction/{faction_id}/rankedwars")
        raw = body.get("rankedwars") if isinstance(body, dict) else None
        rows: list[dict[str, Any]] = []
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            rows = [v for v in raw.values() if isinstance(v, dict)]
        wars = [w for w in (_parse_ranked_war(r) for r in rows) if w is not None]
        return wars

    async def get_current_ranked_war(self, faction_id: int) -> RankedWar | None:
        """Return the most recent ranked war involving ``faction_id``.

        This is the newest entry in the ``/rankedwars`` list. Classifying it
        (no war / pending / active / finished) is :func:`app.war.detector.assess`'s
        job — this method just returns the raw war, or ``None`` if the faction
        has never had a ranked war.
        """
        wars = await self.list_ranked_wars(faction_id)
        for war in wars:  # list is newest-first
            if war.includes(faction_id):
                return war
        return None

    async def has_live_ranked_war(self, faction_id: int) -> bool | None:
        """Secondary signal from ``/faction/{id}/wars`` -> ``wars.ranked``.

        Returns ``True``/``False`` when the field is present, or ``None`` if the
        endpoint response did not contain it (so callers can ignore an
        inconclusive result rather than treat it as "no war").
        """
        body = await self._client.get(f"/faction/{faction_id}/wars")
        wars = body.get("wars") if isinstance(body, dict) else None
        if not isinstance(wars, dict) or "ranked" not in wars:
            return None
        return wars["ranked"] is not None
