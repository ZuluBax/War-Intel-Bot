"""Tiny in-process TTL cache.

Used to avoid re-requesting information that has not changed (spec §19/§26) —
e.g. the enemy faction's member list within a refresh window. It is intentionally
process-local: the bot runs as a single instance, so a Redis dependency would be
premature (spec §35). If the bot is ever horizontally scaled, swap this module's
implementation for a shared cache behind the same interface.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass
class _Entry[T]:
    value: T
    expires_at: float


class TTLCache[T]:
    """Async-safe key/value cache where every entry expires after ``ttl`` seconds."""

    def __init__(self, *, ttl: float, name: str = "cache") -> None:
        self._ttl = ttl
        self._name = name
        self._store: dict[str, _Entry[T]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> T | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: T, *, ttl: float | None = None) -> None:
        self._store[key] = _Entry(value=value, expires_at=time.monotonic() + (ttl or self._ttl))

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    async def get_or_set(
        self, key: str, factory: Callable[[], Awaitable[T]], *, ttl: float | None = None
    ) -> T:
        """Return the cached value, or call ``factory`` once and cache its result.

        A per-key lock ensures that N concurrent misses trigger only one
        ``factory`` call (cache stampede protection).
        """
        hit = self.get(key)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self.get(key)
            if hit is not None:
                return hit
            value = await factory()
            self.set(key, value, ttl=ttl)
            return value
