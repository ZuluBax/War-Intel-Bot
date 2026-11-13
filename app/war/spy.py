"""Confirmed spy reports (spec §24): reading a pasted report, and choosing between a
report and the FFScouter estimate.

Pure functions — no I/O. Reports are stored apart from estimates (``confirmed_stats``
vs ``stat_estimates``) and are never written into the estimate history; this module
only decides which one a *display or ranking* should use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.utils.time import utcnow

# A spy report this recent is trusted over the estimate. Stats only ever go up, so an
# old report undersells the player; past this age the estimate is the better guess.
SPY_TRUSTED_FOR = timedelta(days=14)

# Torn's own spy text ("Strength: 1,234,567"), TornStats/YATA exports ("Total Battle
# Stats: 4.5b"), and Discord's habit of folding a pasted block onto one line all match.
_STAT = re.compile(
    r"\b(?P<stat>strength|str|defen[cs]e|def|speed|spd|dexterity|dex|total)\b"
    r"(?:\s*battle\s*stats?)?\s*[:=\-]?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>[kmbtq](?![a-z]))?",
    re.I,
)
_ID = re.compile(r"\[(\d{1,10})\]|XID=(\d{1,10})", re.I)
_UNIT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12, "q": 1e15}
_FIELD = {
    "strength": "strength",
    "str": "strength",
    "defense": "defense",
    "defence": "defense",
    "def": "defense",
    "speed": "speed",
    "spd": "speed",
    "dexterity": "dexterity",
    "dex": "dexterity",
    "total": "total",
}
# Far above any real Torn stat total; anything bigger is a paste mangled into one number.
_MAX_STAT = 1e16


@dataclass(frozen=True)
class SpyReport:
    strength: float | None = None
    defense: float | None = None
    speed: float | None = None
    dexterity: float | None = None
    total: float | None = None
    player_id: int | None = None  # a Torn ID found in the text, to catch a wrong paste

    @property
    def is_empty(self) -> bool:
        return self.best_total is None and all(
            v is None for v in (self.strength, self.defense, self.speed, self.dexterity)
        )

    @property
    def best_total(self) -> float | None:
        """The stated total, else the sum when all four stats are known."""
        if self.total is not None:
            return self.total
        parts = (self.strength, self.defense, self.speed, self.dexterity)
        if any(p is None for p in parts):
            return None
        return sum(p for p in parts if p is not None)


def _number(raw: str, unit: str | None) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    value *= _UNIT.get((unit or "").lower(), 1.0)
    return value if 0 <= value <= _MAX_STAT else None


def parse_spy_report(text: str) -> SpyReport:
    """Read the battle stats out of pasted spy text. The first value for each stat
    wins; "N/A" or missing stats stay None. Never raises."""
    found: dict[str, float] = {}
    for match in _STAT.finditer(text or ""):
        field = _FIELD[match.group("stat").lower()]
        if field in found:
            continue
        value = _number(match.group("num"), match.group("unit"))
        if value is not None:
            found[field] = value
    id_match = _ID.search(text or "")
    player_id = int(id_match.group(1) or id_match.group(2)) if id_match else None
    return SpyReport(player_id=player_id, **found)


@dataclass(frozen=True)
class Strength:
    """The battle-strength number to show/rank on, and where it came from."""

    value: float | None
    confirmed: bool  # True = a trusted spy report, False = the FFScouter estimate


def best_strength(
    *,
    estimate: float | None,
    spy_total: float | None,
    spy_reported_at: datetime | None,
    now: datetime | None = None,
) -> Strength:
    """A trusted (recent) spy total beats the estimate; otherwise use the estimate."""
    if spy_total is not None and spy_reported_at is not None:
        if (now or utcnow()) - spy_reported_at <= SPY_TRUSTED_FOR:
            return Strength(spy_total, confirmed=True)
    return Strength(estimate, confirmed=False)
