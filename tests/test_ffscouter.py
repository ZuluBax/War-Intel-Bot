from __future__ import annotations

import httpx
import pytest
import respx

from app.ffscouter.client import FFScouterClient
from app.ffscouter.errors import FFScouterAuthError, FFScouterError
from app.services.rate_limiter import AsyncRateLimiter, RateLimit

BASE = "https://ffscouter.com/api/v1"


def _client() -> FFScouterClient:
    return FFScouterClient(
        api_key="ffsc0123456789ab",
        base_url=BASE,
        http_client=httpx.AsyncClient(),
        rate_limiter=AsyncRateLimiter(RateLimit(rate=1000, per=1.0), name="test"),
    )


@respx.mock
async def test_parses_stats_rows() -> None:
    respx.get(f"{BASE}/get-stats").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "player_id": 1,
                    "fair_fight": 2.14,
                    "bs_estimate": 4_210_000_000,
                    "bs_estimate_human": "4.21b",
                    "bss_public": 123.0,
                    "last_updated": 1_700_000_000,
                    "source": "bss",
                    "premium_insights_available": False,
                },
                {"player_id": 2},
            ],
        )
    )
    async with _client() as client:
        result = await client.get_stats([1, 2, 2])
    assert result[1].bs_estimate == 4_210_000_000
    assert result[1].last_updated is not None
    assert result[2].has_estimate is False


@respx.mock
async def test_sends_key_and_targets() -> None:
    route = respx.get(f"{BASE}/get-stats").mock(return_value=httpx.Response(200, json=[]))
    async with _client() as client:
        await client.get_stats([3, 1, 2])
    request = route.calls.last.request
    assert request.url.params["key"] == "ffsc0123456789ab"
    assert request.url.params["targets"] == "1,2,3"  # sorted + de-duped


@respx.mock
async def test_batches_over_200_ids() -> None:
    route = respx.get(f"{BASE}/get-stats").mock(return_value=httpx.Response(200, json=[]))
    async with _client() as client:
        await client.get_stats(list(range(1, 451)))
    assert route.call_count == 3  # 200 + 200 + 50


@respx.mock
async def test_missing_ids_still_present_in_result() -> None:
    respx.get(f"{BASE}/get-stats").mock(return_value=httpx.Response(200, json=[]))
    async with _client() as client:
        result = await client.get_stats([10, 20])
    assert set(result) == {10, 20}
    assert all(not r.has_estimate for r in result.values())


@respx.mock
async def test_auth_error_code_6() -> None:
    respx.get(f"{BASE}/get-stats").mock(
        return_value=httpx.Response(401, json={"error": {"code": 6, "message": "bad key"}})
    )
    async with _client() as client:
        with pytest.raises(FFScouterAuthError):
            await client.get_stats([1])


@respx.mock
async def test_non_array_body_raises() -> None:
    respx.get(f"{BASE}/get-stats").mock(return_value=httpx.Response(200, json={"unexpected": 1}))
    async with _client() as client:
        with pytest.raises(FFScouterError):
            await client.get_stats([1])
