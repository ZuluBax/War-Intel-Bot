"""Runs war detection for every configured Discord server, once per scheduler tick.

Each server has its own Torn API key, so it gets its own :class:`TornClient`
with its own rate-limit budget (Torn's 100/min is per key). FFScouter's limit is
per IP, so a single shared limiter is threaded into every server's
:class:`FFScouterClient`.

Per-server managers are cached and rebuilt only when that server's ``/setup``
config changes (tracked by a cheap signature).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.config.guild_config import GuildConfigService, ResolvedGuildConfig
from app.config.settings import Settings
from app.database.database import Database
from app.discord.gateway import GuildGateway
from app.discord.war_channels import WarBoard, WarChannelManager, WarLogBoard
from app.discord.war_publish import DiscordWarPublisher
from app.ffscouter.client import FFScouterClient
from app.ffscouter.errors import FFScouterError
from app.services.rate_limiter import FFSCOUTER_RATE_LIMIT, AsyncRateLimiter
from app.torn.attacks import AttackService
from app.torn.client import TornClient
from app.torn.faction import FactionService
from app.torn.warfare import WarfareService
from app.utils.logging import get_logger
from app.utils.time import utcnow
from app.war.activity_service import ActivityService
from app.war.detector import WarDetector
from app.war.flights import FLIGHT_UNIT_LIMIT, FlightTracker
from app.war.hits import WarHitService
from app.war.hospital_watch import HospitalWatch
from app.war.manager import WarManager
from app.war.roster import EnemyRosterService
from app.war.target_service import TargetingService

logger = get_logger(__name__)

# /matchups asks FFScouter for one of *our* players on demand. FFScouter itself refreshes
# an estimate far less often than this, so re-asking sooner would only spend the shared
# 20/min budget the roster refreshes need.
_ESTIMATE_CACHE_FOR = timedelta(minutes=30)


class RefreshOutcome(StrEnum):
    """Result of a manually-triggered ``/refresh`` (spec §32)."""

    REFRESHED = "refreshed"
    RATE_LIMITED = "rate_limited"
    NOT_CONFIGURED = "not_configured"
    NO_ACTIVE_WAR = "no_active_war"


# make_gateway(guild_id) -> GuildGateway
GatewayMaker = Callable[[int], GuildGateway]


def _signature(cfg: ResolvedGuildConfig) -> str:
    parts = [
        str(cfg.torn_faction_id),
        hashlib.sha256(cfg.torn_api_key.encode()).hexdigest()[:12],
        hashlib.sha256((cfg.ffscouter_api_key or "").encode()).hexdigest()[:12],
        str(cfg.war_intel_role_id),
        str(cfg.war_room_channel_id),
        str(cfg.board_channels),
    ]
    return "|".join(parts)


@dataclass
class _Cached:
    signature: str
    manager: WarManager
    torn_client: TornClient
    ffscouter_client: FFScouterClient | None

    async def aclose(self) -> None:
        await self.torn_client.aclose()
        if self.ffscouter_client is not None:
            await self.ffscouter_client.aclose()


class MultiGuildWarRunner:
    def __init__(
        self,
        database: Database,
        guild_config: GuildConfigService,
        settings: Settings,
        *,
        make_gateway: GatewayMaker,
    ) -> None:
        self._db = database
        self._guild_config = guild_config
        self._settings = settings
        self._make_gateway = make_gateway
        self._ffscouter_limiter = AsyncRateLimiter(FFSCOUTER_RATE_LIMIT, name="ffscouter-shared")
        self._cache: dict[int, _Cached] = {}
        # FFScouter's flight budget is per FFScouter *account*, and several servers can
        # share one key (the operator default) — so one limiter per key, not per server.
        self._flight_limiters: dict[str, AsyncRateLimiter] = {}
        # Manual /refresh cooldown, per server — avoids a spam button hammering
        # Torn/FFScouter. Reuses the status-refresh interval as the cooldown so a
        # manual refresh is never more expensive than the automatic cadence.
        self._last_manual_refresh: dict[int, datetime] = {}
        # torn_player_id -> (when fetched, FFScouter estimate or None)
        self._estimate_cache: dict[int, tuple[datetime, float | None]] = {}
        # One lock per server. The war-detect, status, stats and /refresh jobs run on
        # independent schedules and all read-then-write the same war's rows (roster
        # diff -> join/leave events, target ranking replace); overlapping, they double
        # post and duplicate rows. Kept here, not on WarManager, because a config
        # change rebuilds the manager and a lock on it would silently be replaced.
        self._guild_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    async def tick(self) -> None:
        configs = await self._guild_config.list_ready()
        ready_ids = {c.guild_id for c in configs}

        for gone in [gid for gid in self._cache if gid not in ready_ids]:
            async with self._lock_for(gone):  # don't close clients a running job is using
                cached = self._cache.pop(gone, None)
                if cached is not None:
                    await cached.aclose()

        for cfg in configs:
            try:
                async with self._lock_for(cfg.guild_id):
                    await self._manager_for(cfg).tick()
            except Exception as exc:  # one server's failure must not stop the rest
                logger.error(
                    "multi_guild.tick_failed",
                    guild_id=cfg.guild_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )

    async def refresh_tick(self, *, include_stats: bool) -> None:
        """Periodic roster/stats/status refresh for every ready server (Phase 5)."""
        configs = await self._guild_config.list_ready()
        for cfg in configs:
            lock = self._lock_for(cfg.guild_id)
            if not include_stats and lock.locked():
                # The frequent status pass just skips a busy server and catches up next
                # interval (seconds away). Queueing instead would back the job up behind
                # a slow stats pass until APScheduler logs "maximum number of instances
                # reached" on every firing.
                continue
            try:
                async with lock:
                    await self._manager_for(cfg).refresh_active(include_stats=include_stats)
            except Exception as exc:  # one server's failure must not stop the rest
                logger.error(
                    "multi_guild.refresh_failed",
                    guild_id=cfg.guild_id,
                    include_stats=include_stats,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )

    async def manual_refresh(self, guild_id: int) -> RefreshOutcome:
        """Back the ``/refresh`` command — one server, rate-limited, on demand."""
        cfg = await self._guild_config.resolve(guild_id)
        if cfg is None:
            return RefreshOutcome.NOT_CONFIGURED

        now = utcnow()
        cooldown = self._settings.status_refresh_interval_seconds
        last = self._last_manual_refresh.get(guild_id)
        if last is not None and (now - last).total_seconds() < cooldown:
            return RefreshOutcome.RATE_LIMITED
        self._last_manual_refresh[guild_id] = now

        # The cooldown is checked first, so spamming /refresh can't queue up behind the lock.
        async with self._lock_for(guild_id):
            roster = await self._manager_for(cfg).refresh_active(include_stats=True)
        return RefreshOutcome.REFRESHED if roster is not None else RefreshOutcome.NO_ACTIVE_WAR

    async def strength_estimate(self, guild_id: int, torn_player_id: int) -> float | None:
        """FFScouter's battle-strength estimate for any player, via this server's key.

        None when the server has no FFScouter key, FFScouter has no estimate, or the
        call fails (logged) — callers say "no estimate" rather than erroring.
        """
        now = utcnow()
        cached = self._estimate_cache.get(torn_player_id)
        if cached is not None and now - cached[0] < _ESTIMATE_CACHE_FOR:
            return cached[1]
        cfg = await self._guild_config.resolve(guild_id)
        if cfg is None or not cfg.ffscouter_api_key:
            return None
        self._manager_for(cfg)  # builds (or reuses) this server's clients
        client = self._cache[cfg.guild_id].ffscouter_client
        if client is None:
            return None
        try:
            stats = await client.get_stats([torn_player_id])
        except FFScouterError as exc:
            logger.warning(
                "multi_guild.estimate_failed",
                guild_id=guild_id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            return None
        value = stats[torn_player_id].bs_estimate if torn_player_id in stats else None
        self._estimate_cache[torn_player_id] = (now, value)
        return value

    def _manager_for(self, cfg: ResolvedGuildConfig) -> WarManager:
        sig = _signature(cfg)
        cached = self._cache.get(cfg.guild_id)
        if cached is not None and cached.signature == sig:
            return cached.manager
        if cached is not None:
            self._cache.pop(cfg.guild_id)  # config changed — rebuild

        torn_client = TornClient(
            api_key=cfg.torn_api_key, base_url=self._settings.torn_api_base_url
        )
        ffscouter_client: FFScouterClient | None = None
        if cfg.ffscouter_api_key:
            ffscouter_client = FFScouterClient(
                api_key=cfg.ffscouter_api_key,
                base_url=self._settings.ffscouter_api_base_url,
                rate_limiter=self._ffscouter_limiter,
            )

        roster_service = EnemyRosterService(
            self._db,
            FactionService(torn_client),
            ffscouter_client,
            # Three status refreshes, so one missed poll doesn't lose a flight's take-off.
            observation_gap=timedelta(seconds=3 * self._settings.status_refresh_interval_seconds),
        )
        targeting_service = TargetingService(self._db)
        flight_tracker: FlightTracker | None = None
        if ffscouter_client is not None and cfg.ffscouter_api_key:
            key_id = hashlib.sha256(cfg.ffscouter_api_key.encode()).hexdigest()[:16]
            limiter = self._flight_limiters.setdefault(
                key_id, AsyncRateLimiter(FLIGHT_UNIT_LIMIT, name="ffscouter-flights")
            )
            flight_tracker = FlightTracker(ffscouter_client, limiter)
        gateway = self._make_gateway(cfg.guild_id)
        publisher = DiscordWarPublisher(
            self._db,
            WarChannelManager(
                self._db,
                gateway,
                single_channel_id=cfg.war_room_channel_id,
                board_channels=dict(cfg.board_channels),
            ),
            WarBoard(self._db, gateway),
            WarLogBoard(self._db, gateway),
        )
        manager = WarManager(
            self._db,
            WarDetector(WarfareService(torn_client), our_faction_id=cfg.torn_faction_id),
            guild_id=cfg.guild_id,
            our_faction_id=cfg.torn_faction_id,
            roster_service=roster_service,
            targeting_service=targeting_service,
            hit_service=WarHitService(
                self._db, AttackService(torn_client), our_faction_id=cfg.torn_faction_id
            ),
            publisher=publisher,
            flight_tracker=flight_tracker,
            activity_service=ActivityService(self._db),
            hospital_watch=HospitalWatch(),
        )
        self._cache[cfg.guild_id] = _Cached(
            signature=sig,
            manager=manager,
            torn_client=torn_client,
            ffscouter_client=ffscouter_client,
        )
        logger.info(
            "multi_guild.manager_built",
            guild_id=cfg.guild_id,
            faction_id=cfg.torn_faction_id,
        )
        return manager

    async def aclose(self) -> None:
        for cached in list(self._cache.values()):
            await cached.aclose()
        self._cache.clear()
