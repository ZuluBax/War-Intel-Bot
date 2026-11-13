from __future__ import annotations

import asyncio

from app.services.cache import TTLCache


async def test_get_or_set_calls_factory_once_under_concurrency() -> None:
    cache: TTLCache[int] = TTLCache(ttl=60, name="t")
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return 42

    results = await asyncio.gather(*(cache.get_or_set("k", factory) for _ in range(10)))
    assert results == [42] * 10
    assert calls == 1


async def test_entry_expires() -> None:
    # Wide margin (20ms TTL vs 150ms wait) so the test can't flake under load.
    cache: TTLCache[str] = TTLCache(ttl=0.02, name="t")
    cache.set("k", "v")
    assert cache.get("k") == "v"
    await asyncio.sleep(0.15)
    assert cache.get("k") is None


async def test_invalidate_and_clear() -> None:
    cache: TTLCache[int] = TTLCache(ttl=60, name="t")
    cache.set("a", 1)
    cache.set("b", 2)
    cache.invalidate("a")
    assert cache.get("a") is None
    assert cache.get("b") == 2
    cache.clear()
    assert cache.get("b") is None
