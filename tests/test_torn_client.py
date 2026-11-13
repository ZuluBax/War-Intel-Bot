from __future__ import annotations

import httpx
import pytest
import respx

from app.services.rate_limiter import AsyncRateLimiter, RateLimit
from app.torn.client import TornClient
from app.torn.errors import (
    TornAPIAccessLevelError,
    TornAPIAuthError,
    TornAPIError,
    TornAPIRateLimitError,
)

BASE = "https://api.torn.com/v2"


def _client() -> TornClient:
    # Generous limiter so tests never actually sleep on the bucket.
    return TornClient(
        api_key="0123456789abcdef",
        base_url=BASE,
        http_client=httpx.AsyncClient(),
        rate_limiter=AsyncRateLimiter(RateLimit(rate=1000, per=1.0), name="test"),
    )


@respx.mock
async def test_get_returns_parsed_body() -> None:
    respx.get(f"{BASE}/faction/12345/basic").mock(
        return_value=httpx.Response(200, json={"basic": {"id": 12345, "name": "Us"}})
    )
    async with _client() as client:
        body = await client.get("/faction/12345/basic")
    assert body["basic"]["name"] == "Us"


@respx.mock
async def test_sends_v2_apikey_header() -> None:
    route = respx.get(f"{BASE}/torn/rankedwars").mock(
        return_value=httpx.Response(200, json={"rankedwars": {}})
    )
    async with _client() as client:
        await client.get("/torn/rankedwars")
    assert route.calls.last.request.headers["Authorization"] == "ApiKey 0123456789abcdef"


@respx.mock
@pytest.mark.parametrize(
    ("code", "exc"),
    [
        (2, TornAPIAuthError),
        (5, TornAPIRateLimitError),
        (16, TornAPIAccessLevelError),
    ],
)
async def test_maps_error_codes(code: int, exc: type[Exception]) -> None:
    respx.get(f"{BASE}/faction/1/basic").mock(
        return_value=httpx.Response(200, json={"error": {"code": code, "error": "nope"}})
    )
    async with _client() as client:
        with pytest.raises(exc):
            await client.get("/faction/1/basic")


@respx.mock
async def test_retries_transient_then_succeeds() -> None:
    route = respx.get(f"{BASE}/faction/1/basic")
    route.side_effect = [
        httpx.Response(200, json={"error": {"code": 17, "error": "temporary"}}),
        httpx.Response(200, json={"basic": {"id": 1}}),
    ]
    async with _client() as client:
        body = await client.get("/faction/1/basic")
    assert body["basic"]["id"] == 1
    assert route.call_count == 2


@respx.mock
async def test_non_json_response_raises() -> None:
    respx.get(f"{BASE}/faction/1/basic").mock(return_value=httpx.Response(200, text="<html>"))
    async with _client() as client:
        with pytest.raises(TornAPIError):
            await client.get("/faction/1/basic")
