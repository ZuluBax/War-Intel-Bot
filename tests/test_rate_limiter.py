from __future__ import annotations

import asyncio
import time

import pytest

from app.services.rate_limiter import AsyncRateLimiter, RateLimit


async def test_allows_burst_up_to_capacity() -> None:
    limiter = AsyncRateLimiter(RateLimit(rate=5, per=1.0), name="t")
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    assert time.monotonic() - start < 0.05  # no waiting for the first `capacity`


async def test_blocks_once_bucket_is_empty() -> None:
    limiter = AsyncRateLimiter(RateLimit(rate=10, per=1.0), name="t")  # refill 10/s
    for _ in range(10):
        await limiter.acquire()
    start = time.monotonic()
    await limiter.acquire()  # must wait ~0.1s for one token
    assert time.monotonic() - start >= 0.08


async def test_try_acquire_is_non_blocking() -> None:
    limiter = AsyncRateLimiter(RateLimit(rate=2, per=10.0), name="t")
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False


async def test_requesting_more_than_capacity_raises() -> None:
    limiter = AsyncRateLimiter(RateLimit(rate=3, per=1.0), name="t")
    with pytest.raises(ValueError):
        await limiter.acquire(4)


async def test_concurrent_acquires_are_serialised() -> None:
    limiter = AsyncRateLimiter(RateLimit(rate=4, per=1.0), name="t")
    await asyncio.gather(*(limiter.acquire() for _ in range(4)))
    assert limiter.try_acquire() is False
