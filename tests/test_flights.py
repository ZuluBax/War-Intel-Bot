"""FFScouter premium flight windows: client parsing, the tracker, and the board."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.discord.target_board import target_board_embed
from app.ffscouter.client import FFScouterClient
from app.ffscouter.errors import FFScouterPremiumRequiredError
from app.ffscouter.models import FlightWindow
from app.services.rate_limiter import AsyncRateLimiter, RateLimit
from app.torn.models import PlayerState
from app.war.flights import FlightTracker
from app.war.live_status import StatusGroup, live_status
from app.war.target_service import TargetView

BASE = "https://ffscouter.com/api/v1"
_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _client() -> FFScouterClient:
    return FFScouterClient(
        api_key="ffsc0123456789ab",
        base_url=BASE,
        http_client=httpx.AsyncClient(),
        rate_limiter=AsyncRateLimiter(RateLimit(rate=1000, per=1.0), name="test"),
    )


# --- client --------------------------------------------------------------------------


@respx.mock
async def test_get_flights_parses_documented_example() -> None:
    # The batch example from https://ffscouter.com/api-docs, verbatim.
    route = respx.get(f"{BASE}/player-flights/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "flights": [
                    {
                        "player_id": 1844049,
                        "current": {
                            "takeoff_time": 1710000000,
                            "status_description": "Traveling to Japan",
                            "earliest_arrival_time": 1710007200,
                            "latest_arrival_time": 1710010800,
                            "travel_method": "Airline",
                            "book_likely_being_used": True,
                        },
                    },
                    {"player_id": 2675763, "current": None},
                ]
            },
        )
    )
    async with _client() as client:
        result = await client.get_flights([2675763, 1844049, 1844049])
    assert route.calls.last.request.url.params["targets"] == "1844049,2675763"
    window = result[1844049]
    assert window is not None
    assert window.earliest_arrival == datetime.fromtimestamp(1710007200, UTC)
    assert window.latest_arrival == datetime.fromtimestamp(1710010800, UTC)
    assert window.travel_method == "Airline"
    assert window.book_likely is True
    assert result[2675763] is None


@respx.mock
async def test_get_flights_premium_required() -> None:
    respx.get(f"{BASE}/player-flights/batch").mock(
        return_value=httpx.Response(
            403, json={"error": {"code": 19, "message": "Active premium required"}}
        )
    )
    async with _client() as client:
        with pytest.raises(FFScouterPremiumRequiredError):
            await client.get_flights([1])


@respx.mock
async def test_get_flights_premium_required_live_shape() -> None:
    # Exactly what the live API returned for a non-premium key on 22 Sep 2026.
    respx.get(f"{BASE}/player-flights/batch").mock(
        return_value=httpx.Response(
            403,
            json={"code": 19, "error": "Active premium subscription required to use this endpoint"},
        )
    )
    async with _client() as client:
        with pytest.raises(FFScouterPremiumRequiredError):
            await client.get_flights([1])


@respx.mock
async def test_get_flights_malformed_flight_is_none() -> None:
    respx.get(f"{BASE}/player-flights/batch").mock(
        return_value=httpx.Response(
            200,
            json={"flights": [{"player_id": 1, "current": {"takeoff_time": "soon"}}]},
        )
    )
    async with _client() as client:
        assert (await client.get_flights([1]))[1] is None


# --- tracker -------------------------------------------------------------------------


def _view(pid: int, description: str, state: PlayerState = PlayerState.TRAVELING) -> TargetView:
    return TargetView(
        player_id=pid,
        name=f"Enemy{pid}",
        level=50,
        rank=pid,
        score=0.5,
        priority="LOW",
        state=state,
        is_available=state is PlayerState.OKAY,
        status_description=description,
    )


_WINDOW = FlightWindow(
    takeoff_at=_NOW - timedelta(minutes=30),
    earliest_arrival=_NOW + timedelta(minutes=10),
    latest_arrival=_NOW + timedelta(minutes=14),
    travel_method="WLT",
)


class _FakeClient:
    def __init__(self, result: dict[int, FlightWindow | None] | Exception) -> None:
        self.result = result
        self.calls: list[list[int]] = []

    async def get_flights(self, ids: list[int]) -> dict[int, FlightWindow | None]:
        self.calls.append(list(ids))
        if isinstance(self.result, Exception):
            raise self.result
        return {i: self.result.get(i) for i in ids}


def _tracker(client: _FakeClient, rate: int = 90) -> FlightTracker:
    return FlightTracker(client, AsyncRateLimiter(RateLimit(rate=rate, per=60.0)))  # type: ignore[arg-type]


async def test_tracker_only_asks_about_players_in_the_air() -> None:
    client = _FakeClient({1: _WINDOW})
    targets = [
        _view(1, "Traveling from Japan to Torn"),
        _view(2, "In Japan"),  # landed abroad
        _view(3, "Okay", PlayerState.OKAY),
    ]
    out = await _tracker(client).enrich(targets, now=_NOW)
    assert client.calls == [[1]]
    assert out[0].flight == _WINDOW
    assert out[1].flight is None and out[2].flight is None


async def test_tracker_caches_until_flight_changes_or_recheck() -> None:
    client = _FakeClient({1: _WINDOW})
    tracker = _tracker(client)
    flying = [_view(1, "Traveling from Japan to Torn")]
    await tracker.enrich(flying, now=_NOW)
    out = await tracker.enrich(flying, now=_NOW + timedelta(minutes=1))
    assert len(client.calls) == 1  # cached
    assert out[0].flight == _WINDOW
    await tracker.enrich(
        [_view(1, "Traveling from Torn to Mexico")], now=_NOW + timedelta(minutes=2)
    )
    assert len(client.calls) == 2  # new flight -> asked again
    await tracker.enrich(
        [_view(1, "Traveling from Torn to Mexico")], now=_NOW + timedelta(minutes=8)
    )
    assert len(client.calls) == 3  # recheck interval passed


async def test_tracker_switches_off_without_premium() -> None:
    client = _FakeClient(FFScouterPremiumRequiredError("no premium"))
    tracker = _tracker(client)
    flying = [_view(1, "Traveling from Japan to Torn")]
    out = await tracker.enrich(flying, now=_NOW)
    assert out[0].flight is None
    await tracker.enrich(flying, now=_NOW + timedelta(minutes=30))
    assert len(client.calls) == 1  # still off
    await tracker.enrich(flying, now=_NOW + timedelta(minutes=61))
    assert len(client.calls) == 2  # tries again after an hour


async def test_tracker_skips_when_out_of_budget() -> None:
    client = _FakeClient({})
    out = await _tracker(client, rate=1).enrich(
        [_view(1, "Traveling from Japan to Torn"), _view(2, "Traveling from UAE to Torn")],
        now=_NOW,
    )
    assert client.calls == []  # never blocks waiting for budget
    assert all(t.flight is None for t in out)


async def test_tracker_ignores_window_long_past() -> None:
    old = FlightWindow(
        takeoff_at=_NOW - timedelta(hours=3),
        earliest_arrival=_NOW - timedelta(minutes=40),
        latest_arrival=_NOW - timedelta(minutes=30),
    )
    out = await _tracker(_FakeClient({1: old})).enrich(
        [_view(1, "Traveling from Japan to Torn")], now=_NOW
    )
    assert out[0].flight is None


# --- live status + board -------------------------------------------------------------


def test_live_status_uses_ffscouter_when_take_off_not_seen() -> None:
    status = live_status(
        PlayerState.TRAVELING,
        "Traveling from Japan to Torn",
        None,
        "light_aircraft",
        None,  # we never saw this flight take off
        _WINDOW,
    )
    assert status.group is StatusGroup.INCOMING
    (w,) = status.windows
    assert (w.earliest, w.latest, w.method) == (
        _WINDOW.earliest_arrival,
        _WINDOW.latest_arrival,
        "WLT",
    )
    assert status.from_ffscouter


def test_live_status_without_window_is_unchanged() -> None:
    status = live_status(
        PlayerState.TRAVELING, "Traveling from Japan to Torn", None, None, None, None
    )
    assert status.windows == ()


def test_live_status_prefers_own_window_when_take_off_seen() -> None:
    status = live_status(
        PlayerState.TRAVELING,
        "Traveling from Japan to Torn",
        None,
        "light_aircraft",
        _NOW - timedelta(minutes=30),
        _WINDOW,
    )
    assert not status.from_ffscouter
    assert status.windows[0].method == "PI"


def _ts(when: datetime, style: str) -> str:
    return f"<t:{int(when.timestamp())}:{style}>"


def test_board_shows_window() -> None:
    view = replace(_view(1, "Traveling from Japan to Torn"), flight=_WINDOW)
    text = target_board_embed([view], now=_NOW)[0].description or ""
    assert (
        f"Japan → Torn · lands 🎯{_ts(_WINDOW.earliest_arrival, 'R')} "
        f"({_ts(_WINDOW.earliest_arrival, 't')}-{_ts(_WINDOW.latest_arrival, 't')}) WLT"
    ) in text


def test_board_window_already_open() -> None:
    view = replace(_view(1, "Traveling from Japan to Torn"), flight=_WINDOW)
    text = target_board_embed([view], now=_NOW + timedelta(minutes=12))[0].description or ""
    assert f"landing now 🎯 (by {_ts(_WINDOW.latest_arrival, 't')})" in text


def test_tracker_skips_flights_we_saw_take_off() -> None:
    import asyncio

    client = _FakeClient({1: _WINDOW})
    seen = replace(_view(1, "Traveling from Japan to Torn"), status_since=_NOW)
    out = asyncio.run(_tracker(client).enrich([seen], now=_NOW))
    assert client.calls == []  # our own window covers it; no FFScouter units spent
    assert out[0].flight is None


def test_board_airliner_shows_both_tickets_until_bct_window_passes() -> None:
    left = _NOW - timedelta(minutes=10)
    view = replace(
        _view(1, "Traveling from Japan to Torn"), plane_type="airliner", status_since=left
    )
    text = target_board_embed([view], now=_NOW)[0].description or ""
    assert " BCT or " in text and text.rstrip().endswith("Airline")
    later = target_board_embed([view], now=left + timedelta(minutes=90))[0].description or ""
    assert "BCT" not in later and "Airline" in later
