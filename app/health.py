"""Minimal inbound HTTP surface: a liveness/readiness probe.

The bot is otherwise a pure outbound Discord-gateway client (see the Dockerfile
comment: "no inbound ports"). Some host platforms need something to poll
instead of just watching the process, e.g. Cloudflare Containers'
``startAndWaitForPorts()`` / ``pingEndpoint``, or a plain Docker
``HEALTHCHECK``. This exists only for them — it carries no application logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from app.utils.logging import get_logger

if TYPE_CHECKING:
    import discord

logger = get_logger("app.health")


class HealthServer:
    """Serves ``GET /health`` -> 200 once the Discord gateway is ready, else 503."""

    def __init__(self, bot: discord.Client, *, port: int) -> None:
        self._bot = bot
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)  # noqa: S104
        await site.start()
        logger.info("health.listening", port=self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_health(self, _request: web.Request) -> web.Response:
        ready = self._bot.is_ready() and not self._bot.is_closed()
        status = 200 if ready else 503
        return web.json_response({"status": "ok" if ready else "starting"}, status=status)
