"""Application entrypoint.

Responsibilities:
    * load + validate configuration (fails fast with a readable message)
    * configure structured logging
    * ensure the database schema exists (MVP bootstrap; prod uses Alembic)
    * build the secrets cipher + per-guild config service
    * start the background scheduler running the multi-server war runner
    * connect the Discord bot and run until interrupted
    * shut everything down cleanly on SIGINT/SIGTERM
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import signal
import sys

from pydantic import ValidationError

from app.config.guild_config import GuildConfigService
from app.config.settings import Settings, get_settings
from app.database.database import get_database
from app.discord.bot import build_bot
from app.discord.gateway import DiscordGuildGateway
from app.discord.services import BotServices
from app.health import HealthServer
from app.services.crypto import SecretCipher
from app.services.scheduler import Scheduler
from app.utils.logging import configure_logging, get_logger
from app.war.history import WarHistoryService
from app.war.intelligence import IntelligenceService
from app.war.multi_guild import MultiGuildWarRunner


async def _amain(settings: Settings) -> int:
    logger = get_logger("app.main")
    logger.info("app.starting", version=_version(), mock_api=settings.mock_api)

    assert settings.discord_token is not None  # verified in run()

    database = get_database()
    await database.create_all()

    cipher = SecretCipher.from_sources(
        settings.config_encryption_key.get_secret_value()
        if settings.config_encryption_key
        else None
    )
    guild_config = GuildConfigService(
        database, cipher, operator_ffscouter_key=settings.operator_ffscouter_key
    )
    # Before anything starts: with the wrong key every server would silently stop.
    if not await guild_config.startup_key_check(
        reset_unreadable=settings.reset_unreadable_keys
    ):
        await database.dispose()
        return 1

    scheduler = Scheduler()
    scheduler.start()

    bot = build_bot(settings)

    runner = MultiGuildWarRunner(
        database,
        guild_config,
        settings,
        make_gateway=lambda guild_id: DiscordGuildGateway(bot, guild_id),
    )
    bot.services = BotServices(
        database=database,
        intelligence=IntelligenceService(),
        guild_config=guild_config,
        runner=runner,
        war_history=WarHistoryService(database),
    )
    if settings.mock_api:
        logger.warning("app.mock_api_enabled", note="war detection/refresh jobs not scheduled")
    else:
        scheduler.add_interval_job(
            runner.tick,
            seconds=settings.war_check_interval_seconds,
            job_id="war-detect",
        )
        scheduler.add_interval_job(
            functools.partial(runner.refresh_tick, include_stats=False),
            seconds=settings.status_refresh_interval_seconds,
            job_id="war-status-refresh",
        )
        scheduler.add_interval_job(
            functools.partial(runner.refresh_tick, include_stats=True),
            seconds=settings.stats_refresh_interval_seconds,
            job_id="war-stats-refresh",
        )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # SIGTERM absent on Windows
            loop.add_signal_handler(sig, stop.set)

    health_server = HealthServer(bot, port=settings.health_port)
    await health_server.start()

    token = settings.discord_token.get_secret_value()
    bot_task = asyncio.create_task(bot.start(token), name="discord-bot")
    stop_task = asyncio.create_task(stop.wait(), name="stop-signal")
    exit_code = 0

    try:
        await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if bot_task.done():
            exc = bot_task.exception()
            if exc is not None:
                logger.error(
                    "app.bot_crashed", error_type=type(exc).__name__, detail=str(exc)
                )
                exit_code = 1
    finally:
        logger.info("app.shutting_down")
        scheduler.shutdown()
        for task in (bot_task, stop_task):
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if not bot.is_closed():
            await bot.close()
        await runner.aclose()
        await health_server.stop()
        await database.dispose()

    logger.info("app.stopped")
    return exit_code


def _version() -> str:
    from app import __version__

    return __version__


def run() -> None:
    """Console-script / ``python -m app.main`` entrypoint."""
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Do not print the raw exception if it could echo a value; pydantic v2
        # error output shows field names + messages, not SecretStr contents.
        print("Configuration error - check your .env file:\n", file=sys.stderr)
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            print(f"  - {loc}: {err['msg']}", file=sys.stderr)
        raise SystemExit(2) from None

    missing = settings.missing_for_run()
    if missing:
        print("Cannot start - these must be set in your .env file:\n", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        raise SystemExit(2)

    configure_logging(settings)
    raise SystemExit(asyncio.run(_amain(settings)))


if __name__ == "__main__":
    run()
