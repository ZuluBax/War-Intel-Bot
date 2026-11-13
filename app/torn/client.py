"""Async Torn API v2 client.

Every Torn API call in the application goes through this one class. It owns:

* the base URL and the ``Authorization: ApiKey <key>`` header (v2 form),
* a shared client-side rate limiter (90/min, under Torn's 100/min per-user cap),
* timeout + bounded retry with exponential backoff for transient failures,
* structured error mapping (see :mod:`app.torn.errors`),
* logging that never includes the API key.

Higher-level helpers (faction, warfare, players) build on ``get()`` — they do
**not** create their own HTTP sessions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config.settings import Settings
from app.services.rate_limiter import TORN_RATE_LIMIT, AsyncRateLimiter
from app.torn.errors import RETRYABLE, TornAPIError, error_for_code
from app.utils.logging import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.5


class TornClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.torn.com/v2",
        http_client: httpx.AsyncClient | None = None,
        rate_limiter: AsyncRateLimiter | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError("Torn API key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._limiter = rate_limiter or AsyncRateLimiter(TORN_RATE_LIMIT, name="torn")

    @classmethod
    def from_settings(
        cls, settings: Settings, *, rate_limiter: AsyncRateLimiter | None = None
    ) -> TornClient:
        """Build from the operator/dev key in ``.env`` (local dev only)."""
        if settings.torn_api_key is None:
            raise ValueError("TORN_API_KEY is not configured in .env")
        return cls(
            api_key=settings.torn_api_key.get_secret_value(),
            base_url=settings.torn_api_base_url,
            rate_limiter=rate_limiter,
        )

    async def __aenter__(self) -> TornClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a Torn v2 endpoint and return its parsed JSON body.

        ``path`` is relative to the v2 base (e.g. ``"/faction/12345/members"``)
        or a full URL (Torn hands back absolute ``_metadata.links.next`` URLs for
        pagination — pass those straight through).

        Raises a :class:`~app.torn.errors.TornAPIError` subclass on any Torn
        error code or unrecoverable transport failure.
        """
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = {"Accept": "application/json", "Authorization": f"ApiKey {self._api_key}"}

        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            await self._limiter.acquire()
            try:
                response = await self._http.get(url, headers=headers, params=params)
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "torn.network_error",
                    attempt=attempt,
                    max_attempts=_MAX_RETRIES,
                    error_type=type(exc).__name__,
                )
                await self._backoff(attempt)
                continue

            try:
                body = response.json()
            except ValueError as exc:
                raise TornAPIError(
                    f"Torn API returned non-JSON (HTTP {response.status_code})"
                ) from exc

            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                api_error = error_for_code(
                    int(err.get("code", 0)), str(err.get("error", "Unknown error"))
                )
                if isinstance(api_error, RETRYABLE) and attempt < _MAX_RETRIES:
                    logger.warning("torn.retryable_error", attempt=attempt, detail=str(api_error))
                    last_exc = api_error
                    await self._backoff(attempt)
                    continue
                raise api_error

            if not response.is_success:
                raise TornAPIError(f"Torn API returned HTTP {response.status_code}")

            return body  # type: ignore[no-any-return]

        assert last_exc is not None
        raise TornAPIError(
            f"Torn API request failed after {_MAX_RETRIES} attempts"
        ) from last_exc

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
