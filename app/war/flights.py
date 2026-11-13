"""Landing windows from FFScouter's premium Flight Tracker, for the target board.

The bot's own landing estimate (``app.war.live_status``) only works when the bot saw the
take-off. FFScouter watches flights across all of Torn, so a **premium** FFScouter key
gets a landing window for most flights the bot never saw begin, plus the travel method.

It is only asked about flights whose take-off the bot did NOT see (bot restarted, or
the player was already in the air when the war was found); for the rest the bot's own
window is used. This is strictly an add-on:

* No premium key (or no key at all) -> the tracker switches itself off for an hour and
  the board keeps using the bot's own estimate. Nothing else changes.
* Any FFScouter failure -> keep the last good windows, log, carry on.

Budget: FFScouter charges 1 "unit" per player looked up, 100 units/minute per FFScouter
account. Several Discord servers can share one key (the operator default), so the unit
limiter is shared per key by the caller. To stay well inside it, a player is only looked
up again when their Torn status text changes (a new flight) or the cached answer is older
than ``_RECHECK``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.ffscouter.client import FFScouterClient
from app.ffscouter.errors import (
    FFScouterAuthError,
    FFScouterError,
    FFScouterPremiumRequiredError,
)
from app.ffscouter.models import FlightWindow
from app.services.rate_limiter import AsyncRateLimiter, RateLimit
from app.torn.models import PlayerState
from app.utils.logging import get_logger
from app.utils.time import utcnow
from app.war.live_status import TravelPhase, parse_travel
from app.war.target_service import TargetView

logger = get_logger(__name__)

# Published: 100 units/min per FFScouter account. Keep 10 spare.
FLIGHT_UNIT_LIMIT = RateLimit(rate=90, per=60.0)

# FFScouter narrows a window as a flight goes on (e.g. once it can tell a book is in
# use), so re-ask occasionally even if the flight itself hasn't changed.
_RECHECK = timedelta(minutes=5)
# How long to stay off after FFScouter says the key has no premium / is invalid.
_DISABLED_FOR = timedelta(hours=1)
# A window that ended this long ago belongs to a flight that is over.
_STALE_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class _Cached:
    description: str | None
    fetched_at: datetime
    window: FlightWindow | None


class FlightTracker:
    def __init__(self, client: FFScouterClient, unit_limiter: AsyncRateLimiter) -> None:
        self._client = client
        self._units = unit_limiter
        self._cache: dict[int, _Cached] = {}
        self._disabled_until: datetime | None = None

    async def enrich(
        self, targets: list[TargetView], *, now: datetime | None = None
    ) -> list[TargetView]:
        """Return ``targets`` with ``flight`` filled in for enemies in the air.

        Never raises: the target board must publish even if FFScouter is down.
        """
        now = now or utcnow()
        flying = {
            t.player_id: t
            for t in targets
            if t.state is PlayerState.TRAVELING
            and (info := parse_travel(t.status_description)) is not None
            and info.phase is not TravelPhase.ABROAD  # landed abroad: nothing to count down
            # We saw the take-off: our own window (app.war.live_status) covers it.
            and t.status_since is None
        }
        # Forget players who have landed, so the cache can't grow across a long war.
        for pid in [p for p in self._cache if p not in flying]:
            del self._cache[pid]
        if not flying:
            return targets

        if self._disabled_until is None or now >= self._disabled_until:
            await self._fetch(flying, now)

        def with_window(t: TargetView) -> TargetView:
            cached = self._cache.get(t.player_id)
            if t.player_id not in flying or cached is None or cached.window is None:
                return t
            if cached.window.latest_arrival + _STALE_AFTER < now:
                return t
            return replace(t, flight=cached.window)

        return [with_window(t) for t in targets]

    async def _fetch(self, flying: dict[int, TargetView], now: datetime) -> None:
        due = [
            pid
            for pid, t in flying.items()
            if (c := self._cache.get(pid)) is None
            or c.description != t.status_description
            or now - c.fetched_at >= _RECHECK
        ]
        # The limiter's bucket holds FLIGHT_UNIT_LIMIT.rate units; anything left over is
        # simply looked up on the next refresh.
        due = sorted(due)[: FLIGHT_UNIT_LIMIT.rate]
        if not due:
            return
        # Never wait for budget: a key shared by many servers must not stall this
        # server's board. Out of budget -> keep the cached windows, try next refresh.
        if not self._units.try_acquire(len(due)):
            logger.info("flights.budget_exhausted", wanted=len(due))
            return
        try:
            windows = await self._client.get_flights(due)
        except (FFScouterPremiumRequiredError, FFScouterAuthError) as exc:
            self._disabled_until = now + _DISABLED_FOR
            logger.info(
                "flights.premium_unavailable",
                reason=type(exc).__name__,
                retry_after_minutes=int(_DISABLED_FOR.total_seconds() // 60),
            )
            return
        except FFScouterError as exc:
            logger.warning("flights.fetch_failed", error_type=type(exc).__name__, detail=str(exc))
            return
        if self._disabled_until is not None:
            logger.info("flights.premium_available")
            self._disabled_until = None
        for pid in due:
            self._cache[pid] = _Cached(
                description=flying[pid].status_description,
                fetched_at=now,
                window=windows.get(pid),
            )
