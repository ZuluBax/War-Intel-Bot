"""Small, defensive parsers for loosely-typed API payloads."""

from __future__ import annotations

from typing import Any


def as_int(value: Any) -> int | None:
    """Return ``value`` as an ``int`` if it plausibly is one, else ``None``.

    ``bool`` is rejected explicitly (it is an ``int`` subclass, and a stray
    ``true`` must never be read as the id ``1``). Numeric strings are accepted
    because Torn is not consistent about quoting ids.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None
