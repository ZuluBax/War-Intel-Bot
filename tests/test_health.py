from __future__ import annotations

import aiohttp

from app.health import HealthServer


class _FakeBot:
    def __init__(self, *, ready: bool, closed: bool = False) -> None:
        self._ready = ready
        self._closed = closed

    def is_ready(self) -> bool:
        return self._ready

    def is_closed(self) -> bool:
        return self._closed


async def _get(port: int) -> tuple[int, dict[str, object]]:
    async with aiohttp.ClientSession() as session, session.get(
        f"http://127.0.0.1:{port}/health"
    ) as resp:
        return resp.status, await resp.json()


async def test_health_returns_503_before_bot_is_ready() -> None:
    server = HealthServer(_FakeBot(ready=False), port=0)
    await server.start()
    try:
        port = server._runner.addresses[0][1]  # type: ignore[union-attr]
        status, body = await _get(port)
        assert status == 503
        assert body == {"status": "starting"}
    finally:
        await server.stop()


async def test_health_returns_200_once_bot_is_ready() -> None:
    server = HealthServer(_FakeBot(ready=True), port=0)
    await server.start()
    try:
        port = server._runner.addresses[0][1]  # type: ignore[union-attr]
        status, body = await _get(port)
        assert status == 200
        assert body == {"status": "ok"}
    finally:
        await server.stop()


async def test_health_returns_503_if_bot_is_closed_even_if_ready() -> None:
    server = HealthServer(_FakeBot(ready=True, closed=True), port=0)
    await server.start()
    try:
        port = server._runner.addresses[0][1]  # type: ignore[union-attr]
        status, _ = await _get(port)
        assert status == 503
    finally:
        await server.stop()
