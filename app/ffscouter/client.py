"""Async FFScouter API v1 client.

Verified contract (docs/api-contracts.md):

* Base URL ``https://ffscouter.com/api/v1``
* ``GET /get-stats?key=<key>&targets=<comma-separated player ids>``
* Up to **205** target IDs per request -> we batch at 200 for headroom.
* **20 requests/minute per IP** -> guarded by a dedicated rate limiter.
* Response: JSON array of objects, one per requested target.
* ``GET /player-flights/batch?key=<key>&targets=<ids>`` — **premium only**, max 100
  ids, 100 "units" (= targets) per minute per FFScouter account. Times are unix seconds.

This client is isolated from :class:`app.torn.client.TornClient` — FFScouter
calls never share Torn's HTTP session or rate budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from app.config.settings import Settings
from app.ffscouter.errors import RETRYABLE, FFScouterError, error_for_code
from app.ffscouter.models import FFScouterStat, FlightWindow
from app.services.rate_limiter import FFSCOUTER_RATE_LIMIT, AsyncRateLimiter
from app.utils.logging import get_logger
from app.utils.time import from_unix

logger = get_logger(__name__)

_BATCH_SIZE = 200
_FLIGHT_BATCH_SIZE = 100  # documented max per /player-flights/batch
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0


class FFScouterClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://ffscouter.com/api/v1",
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not api_key:
            raise ValueError("FFScouter API key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._limiter = rate_limiter or AsyncRateLimiter(FFSCOUTER_RATE_LIMIT, name="ffscouter")

    @classmethod
    def from_settings(
        cls, settings: Settings, *, rate_limiter: AsyncRateLimiter | None = None
    ) -> FFScouterClient:
        if settings.ffscouter_api_key is None:
            raise ValueError("FFSCOUTER_API_KEY is not configured in .env")
        return cls(
            api_key=settings.ffscouter_api_key.get_secret_value(),
            base_url=settings.ffscouter_api_base_url,
            rate_limiter=rate_limiter,
        )

    async def __aenter__(self) -> FFScouterClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def get_stats(self, player_ids: Sequence[int]) -> dict[int, FFScouterStat]:
        """Return an estimate object for every requested player ID.

        Players FFScouter has no data for still appear in the result with all
        estimate fields ``None`` (so callers can distinguish "no estimate" from
        "not requested"). Batches are fetched sequentially to respect the
        20/min IP limit.
        """
        unique_ids = sorted({int(pid) for pid in player_ids})
        out: dict[int, FFScouterStat] = {}
        for start in range(0, len(unique_ids), _BATCH_SIZE):
            batch = unique_ids[start : start + _BATCH_SIZE]
            rows = await self._get_stats_batch(batch)
            for row in rows:
                out[row.player_id] = row
        # Guarantee a row per requested id.
        for pid in unique_ids:
            out.setdefault(pid, FFScouterStat(player_id=pid))
        return out

    async def _get_stats_batch(self, batch: list[int]) -> list[FFScouterStat]:
        params = {"key": self._api_key, "targets": ",".join(str(p) for p in batch)}
        body = await self._get("/get-stats", params)
        if not isinstance(body, list):
            raise FFScouterError("FFScouter /get-stats did not return a JSON array")
        return [self._parse_row(item) for item in body if isinstance(item, dict)]

    async def get_flights(self, player_ids: Sequence[int]) -> dict[int, FlightWindow | None]:
        """Current flight of each player — **premium keys only** (else
        :class:`FFScouterPremiumRequiredError`).

        ``None`` means FFScouter has no flight in progress for that player (not flying,
        or it never saw the take-off). One request per ``_FLIGHT_BATCH_SIZE`` players.
        """
        unique_ids = sorted({int(pid) for pid in player_ids})
        out: dict[int, FlightWindow | None] = {pid: None for pid in unique_ids}
        for start in range(0, len(unique_ids), _FLIGHT_BATCH_SIZE):
            batch = unique_ids[start : start + _FLIGHT_BATCH_SIZE]
            params = {"key": self._api_key, "targets": ",".join(str(p) for p in batch)}
            body = await self._get("/player-flights/batch", params)
            flights = body.get("flights") if isinstance(body, dict) else None
            if not isinstance(flights, list):
                raise FFScouterError("FFScouter /player-flights/batch had no 'flights' list")
            for item in flights:
                if not isinstance(item, dict):
                    continue
                pid = _as_int(item.get("player_id"))
                if pid is not None and pid in out:
                    out[pid] = _parse_flight(item.get("current"))
        return out

    async def _get(self, path: str, params: dict[str, str]) -> object:
        """GET with retries on network errors and rate limits; returns parsed JSON."""
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            await self._limiter.acquire()
            try:
                response = await self._http.get(f"{self._base_url}{path}", params=params)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "ffscouter.network_error", attempt=attempt, error_type=type(exc).__name__
                )
                await self._backoff(attempt)
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise FFScouterError(
                    f"FFScouter returned non-JSON (HTTP {response.status_code})"
                ) from exc

            # Error shapes: {"error": {"code": N, "message": "..."}} (as documented) and
            # {"code": N, "error": "..."} (what the live API actually sent on 22 Sep 2026).
            if isinstance(body, dict) and "error" in body:
                err = body["error"]
                top_code = _as_int(body.get("code"))
                if isinstance(err, dict):
                    api_error = error_for_code(
                        int(err.get("code", 0)),
                        str(err.get("message", err.get("error", "Unknown error"))),
                        http_status=response.status_code,
                    )
                elif top_code is not None:
                    api_error = error_for_code(top_code, str(err), http_status=response.status_code)
                else:
                    api_error = FFScouterError(f"FFScouter error: {err}")
                if isinstance(api_error, RETRYABLE) and attempt < _MAX_RETRIES:
                    logger.warning("ffscouter.retryable_error", attempt=attempt)
                    last_exc = api_error
                    await self._backoff(attempt)
                    continue
                raise api_error

            if not response.is_success:
                raise FFScouterError(f"FFScouter returned HTTP {response.status_code}")
            return body

        assert last_exc is not None
        raise FFScouterError(
            f"FFScouter request failed after {_MAX_RETRIES} attempts"
        ) from last_exc

    @staticmethod
    def _parse_row(item: dict[str, object]) -> FFScouterStat:
        player_id = _as_int(item.get("player_id"))
        if player_id is None:
            raise FFScouterError("FFScouter /get-stats row is missing a numeric player_id")
        raw_updated = item.get("last_updated")
        return FFScouterStat(
            player_id=player_id,
            fair_fight=_as_float(item.get("fair_fight")),
            bs_estimate=_as_float(item.get("bs_estimate")),
            bs_estimate_human=_as_str(item.get("bs_estimate_human")),
            bss_public=_as_float(item.get("bss_public")),
            last_updated=from_unix(raw_updated) if isinstance(raw_updated, (int, float)) else None,
            source=_as_str(item.get("source")),
            premium_insights_available=bool(item.get("premium_insights_available", False)),
        )

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _parse_flight(raw: object) -> FlightWindow | None:
    """One ``current`` flight object. Anything malformed counts as "no flight" — a wrong
    landing window on the board is worse than falling back to our own estimate."""
    if not isinstance(raw, dict):
        return None
    takeoff = raw.get("takeoff_time")
    earliest = raw.get("earliest_arrival_time")
    latest = raw.get("latest_arrival_time")
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in (takeoff, earliest, latest)
    ):
        return None
    if latest < earliest:  # type: ignore[operator]
        return None
    book = raw.get("book_likely_being_used")
    return FlightWindow(
        takeoff_at=from_unix(takeoff),  # type: ignore[arg-type]
        earliest_arrival=from_unix(earliest),  # type: ignore[arg-type]
        latest_arrival=from_unix(latest),  # type: ignore[arg-type]
        travel_method=_as_str(raw.get("travel_method")),
        book_likely=book if isinstance(book, bool) else None,
    )
