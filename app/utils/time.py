"""Timezone helpers.

Rule for the whole codebase:

* Store and compute in UTC (timezone-aware ``datetime`` objects only).
* Convert to ``Europe/London`` **only** when formatting a value for Discord
  display, so DST is handled correctly by the zoneinfo database rather than a
  hard-coded ``+00:00`` / ``+01:00`` offset.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def utcnow() -> datetime:
    """Timezone-aware 'now' in UTC. Use this instead of ``datetime.utcnow()``."""
    return datetime.now(tz=UTC)


def from_unix(seconds: int | float) -> datetime:
    """Convert a Torn/FFScouter epoch-seconds timestamp to an aware UTC datetime."""
    return datetime.fromtimestamp(seconds, tz=UTC)


def to_london(value: datetime) -> datetime:
    """Convert an aware datetime to Europe/London wall-clock time."""
    if value.tzinfo is None:
        raise ValueError("to_london() requires a timezone-aware datetime")
    return value.astimezone(LONDON)


def london_clock(value: datetime) -> str:
    """Render an aware datetime as ``HH:MM:SS`` London time (for embed footers)."""
    return to_london(value).strftime("%H:%M:%S")


def london_date(value: datetime) -> str:
    """Render an aware datetime as ``YYYY-MM-DD`` London date (for history tables)."""
    return to_london(value).strftime("%Y-%m-%d")


def humanise_age(value: datetime, *, now: datetime | None = None) -> str:
    """Return a short 'x minutes ago' style string for an aware UTC datetime."""
    reference = now or utcnow()
    delta = reference - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"
