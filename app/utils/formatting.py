"""Display formatting helpers for Discord output."""

from __future__ import annotations

import re

_CHANNEL_SANITISE_RE = re.compile(r"[^a-z0-9]+")


def format_stat(value: float | int | None) -> str:
    """Format a battle-stat number as a short human string (e.g. ``2.84B``).

    Returns ``"?"`` for ``None`` so callers never have to special-case unknown
    estimates in their layout code.
    """
    if value is None:
        return "?"
    value = float(value)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= threshold:
            return f"{value / threshold:.2f}{suffix}"
    return f"{value:.0f}"


def format_change(current: float | None, previous: float | None) -> str:
    """Return a ``+1.07%`` style change string, or ``"—"`` if not computable."""
    if current is None or previous is None or previous == 0:
        return "—"
    pct = (current - previous) / previous * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.2f}%"


def trend_arrow(current: float | None, previous: float | None, *, epsilon: float = 0.005) -> str:
    """Up/down/flat arrow comparing two estimates (``epsilon`` = 0.5% deadband)."""
    if current is None or previous is None or previous == 0:
        return "•"
    delta = (current - previous) / previous
    if delta > epsilon:
        return "▲"
    if delta < -epsilon:
        return "▼"
    return "▬"


def sanitise_channel_name(raw: str, *, fallback: str = "faction") -> str:
    """Turn an arbitrary faction name into a Discord-safe channel slug.

    Discord lowercases channel names and replaces runs of non-alphanumerics with
    a single hyphen; names are capped at 100 chars. We cap tighter (60) to leave
    room for our fixed prefixes/emoji.
    """
    slug = _CHANNEL_SANITISE_RE.sub("-", raw.strip().lower()).strip("-")
    slug = slug[:60].strip("-")
    return slug or fallback


def truncate(text: str, limit: int) -> str:
    """Truncate to ``limit`` characters with an ellipsis if it was cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
