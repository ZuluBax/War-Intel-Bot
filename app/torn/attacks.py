"""Faction attack feed.

Backed by Torn API v2 (contract verified live on 2026-09-19 — see
docs/api-contracts.md):

* ``GET /v2/faction/{id}/attacks?limit=&sort=&from=&to=`` ->
  ``{"attacks": [ {id, started, ended, attacker{id,name,level,faction{id,name}}|null,
  defender{...}, result, respect_gain, is_ranked_war, is_stealthed, ...} ],
  "_metadata": {...}}``

The feed holds every attack *involving* the faction — hits it made **and** hits
made on it. ``from`` is inclusive and ``limit`` tops out at 100 per page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.torn.client import TornClient
from app.torn.models import AttackRecord
from app.utils.parsing import as_int
from app.utils.time import from_unix

_PAGE_LIMIT = 100
# A page cap keeps one tick's request count bounded (Torn allows ~100/min per
# key) while a long backlog is worked off over successive ticks.
DEFAULT_MAX_PAGES = 5


def _side(raw: Any) -> tuple[int | None, str | None, int | None, str | None]:
    """``(player id, player name, faction id, faction name)`` of one attack party."""
    if not isinstance(raw, dict):
        return None, None, None, None
    faction = raw.get("faction")
    faction = faction if isinstance(faction, dict) else {}
    return as_int(raw.get("id")), raw.get("name"), as_int(faction.get("id")), faction.get("name")


def _parse_attack(raw: dict[str, Any]) -> AttackRecord | None:
    attack_id = as_int(raw.get("id"))
    started = as_int(raw.get("started"))
    if attack_id is None or started is None:
        return None

    attacker_id, attacker_name, attacker_faction_id, _ = _side(raw.get("attacker"))
    defender_id, defender_name, defender_faction_id, defender_faction_name = _side(
        raw.get("defender")
    )
    gain = raw.get("respect_gain")
    respect_gain = (
        float(gain) if isinstance(gain, (int, float)) and not isinstance(gain, bool) else None
    )
    modifiers = raw.get("modifiers")
    warlord_raw = modifiers.get("warlord") if isinstance(modifiers, dict) else None
    warlord = (
        float(warlord_raw)
        if isinstance(warlord_raw, (int, float)) and not isinstance(warlord_raw, bool)
        else None
    )
    return AttackRecord(
        attack_id=attack_id,
        started=from_unix(started),
        attacker_id=attacker_id,
        attacker_name=attacker_name,
        attacker_faction_id=attacker_faction_id,
        defender_id=defender_id,
        defender_name=defender_name,
        defender_faction_id=defender_faction_id,
        defender_faction_name=defender_faction_name,
        result=str(raw.get("result") or "Unknown"),
        respect_gain=respect_gain,
        is_ranked_war=raw.get("is_ranked_war") is True,
        chain=as_int(raw.get("chain")),
        warlord=warlord,
    )


class AttackService:
    def __init__(self, client: TornClient) -> None:
        self._client = client

    async def list_attacks(
        self,
        faction_id: int,
        *,
        since: datetime,
        until: datetime | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[AttackRecord]:
        """Attacks involving ``faction_id`` from ``since`` (inclusive), oldest first.

        Returns at most ``max_pages * 100`` attacks; if the window holds more, the
        caller simply asks again from the newest attack it stored. Raises
        :class:`~app.torn.errors.TornAPIError` if the key cannot read the feed.
        """
        cursor = int(since.timestamp())
        seen: set[int] = set()
        found: list[AttackRecord] = []

        for _ in range(max_pages):
            path = f"/faction/{faction_id}/attacks?limit={_PAGE_LIMIT}&sort=asc&from={cursor}"
            if until is not None:
                path += f"&to={int(until.timestamp())}"
            body = await self._client.get(path)
            rows = body.get("attacks") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                break

            fresh: list[AttackRecord] = []
            for row in rows:
                record = _parse_attack(row) if isinstance(row, dict) else None
                if record is not None and record.attack_id not in seen:
                    seen.add(record.attack_id)
                    fresh.append(record)
            found.extend(fresh)

            if len(rows) < _PAGE_LIMIT or not fresh:
                break
            next_cursor = int(max(r.started for r in fresh).timestamp())
            if next_cursor <= cursor:  # a whole page inside one second — cannot advance
                break
            cursor = next_cursor

        found.sort(key=lambda r: (r.started, r.attack_id))
        return found
