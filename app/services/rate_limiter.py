"""Async token-bucket rate limiter.

One instance guards one upstream API. Torn, FFScouter and Discord each get their
own limiter with their own documented budget (see ``docs/api-contracts.md``),
so a burst of Discord traffic can never eat into the Torn request budget.

The limiter is a *client-side safety cap*. It is not a substitute for handling a
``429`` / Torn error 5 with backoff — that is the client's job — but it makes
hitting those the rare exception rather than the pacing mechanism.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimit:
    """A "``rate`` requests per ``per`` seconds" budget with an optional burst."""

    rate: int
    per: float = 60.0
    burst: int | None = None

    @property
    def capacity(self) -> int:
        return self.burst if self.burst is not None else self.rate


class AsyncRateLimiter:
    """Token bucket. ``await limiter.acquire()`` before every upstream call."""

    def __init__(self, limit: RateLimit, *, name: str = "api") -> None:
        self._limit = limit
        self._name = name
        self._tokens = float(limit.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        refill_rate = self._limit.rate / self._limit.per
        self._tokens = min(self._limit.capacity, self._tokens + elapsed * refill_rate)

    async def acquire(self, tokens: int = 1) -> None:
        """Block until ``tokens`` are available, then consume them."""
        if tokens > self._limit.capacity:
            raise ValueError(
                f"requested {tokens} tokens > bucket capacity {self._limit.capacity}"
            )
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / (self._limit.rate / self._limit.per)
            await asyncio.sleep(wait)

    def try_acquire(self, tokens: int = 1) -> bool:
        """Non-blocking variant. Returns False if the budget is exhausted."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False


# Documented upstream budgets. Deliberately set below the hard published limit so
# a slow round-trip never tips us over. Re-check against docs/api-contracts.md if
# the providers change their limits.
TORN_RATE_LIMIT = RateLimit(rate=90, per=60.0)          # published: 100/min per user
FFSCOUTER_RATE_LIMIT = RateLimit(rate=18, per=60.0)     # published: 20/min per IP
DISCORD_REST_RATE_LIMIT = RateLimit(rate=45, per=1.0, burst=45)  # global ~50/s
