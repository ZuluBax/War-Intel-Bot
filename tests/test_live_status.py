from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.torn.models import PlayerState
from app.war.live_status import (
    StatusGroup,
    TravelInfo,
    TravelPhase,
    classify_flight,
    completed_flight,
    estimate_windows,
    live_status,
    parse_travel,
)

_T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


# --- parse_travel ---------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Traveling from Torn to Japan", TravelInfo(TravelPhase.OUTGOING, "Japan")),
        ("Traveling from Cayman Islands to Torn", TravelInfo(TravelPhase.INCOMING, "Cayman Islands")),
        ("Traveling from South Africa to Torn", TravelInfo(TravelPhase.INCOMING, "South Africa")),
        ("In Argentina", TravelInfo(TravelPhase.ABROAD, "Argentina")),
        ("In UAE", TravelInfo(TravelPhase.ABROAD, "UAE")),
        ("traveling from torn to mexico", TravelInfo(TravelPhase.OUTGOING, "mexico")),
    ],
)
def test_parse_travel_reads_real_torn_descriptions(
    description: str, expected: TravelInfo
) -> None:
    assert parse_travel(description) == expected


@pytest.mark.parametrize(
    "description",
    [None, "", "Okay", "Traveling", "Traveling from Japan to Mexico"],
)
def test_parse_travel_returns_none_when_it_cannot_tell(description: str | None) -> None:
    """Torn only allows trips to and from Torn — anything else is not guessed at."""
    assert parse_travel(description) is None


# --- estimate_windows -----------------------------------------------------


def test_window_is_take_off_plus_the_measured_range_for_the_plane() -> None:
    info = TravelInfo(TravelPhase.OUTGOING, "Japan")
    (w,) = estimate_windows(info, "light_aircraft", _T0)
    assert w.method == "PI"
    # One poll of slack before the fastest time; the slowest time as measured.
    assert w.earliest == _T0 - timedelta(seconds=60) + timedelta(minutes=225 * 0.645 - 1.5)
    assert w.latest == _T0 + timedelta(minutes=225 * 0.692 + 1.5)


def test_airliner_gives_bct_and_normal_ticket_windows_soonest_first() -> None:
    info = TravelInfo(TravelPhase.INCOMING, "Japan")
    bct, airline = estimate_windows(info, "airliner", _T0)
    assert (bct.method, airline.method) == ("BCT", "Airline")
    assert bct.latest < airline.earliest


def test_known_airliner_habit_gives_one_window() -> None:
    info = TravelInfo(TravelPhase.INCOMING, "Japan")
    (w,) = estimate_windows(info, "airliner", _T0, airliner_method="Airline")
    assert w.method == "Airline"


def test_windows_are_case_insensitive_on_the_country() -> None:
    assert estimate_windows(TravelInfo(TravelPhase.OUTGOING, "UAE"), "airliner", _T0)
    assert estimate_windows(TravelInfo(TravelPhase.OUTGOING, "uae"), "airliner", _T0)


@pytest.mark.parametrize(
    ("info", "plane", "departed"),
    [
        (TravelInfo(TravelPhase.OUTGOING, "Japan"), "light_aircraft", None),  # never saw take-off
        (TravelInfo(TravelPhase.OUTGOING, "Japan"), None, _T0),  # plane unknown
        (TravelInfo(TravelPhase.OUTGOING, "Japan"), "hot_air_balloon", _T0),  # plane not known
        (TravelInfo(TravelPhase.OUTGOING, "Atlantis"), "airliner", _T0),  # country not known
        (TravelInfo(TravelPhase.ABROAD, "Japan"), "airliner", _T0),  # not flying
    ],
)
def test_no_window_rather_than_a_guess(
    info: TravelInfo, plane: str | None, departed: datetime | None
) -> None:
    assert estimate_windows(info, plane, departed) == ()


# --- classify_flight / completed_flight -------------------------------------


@pytest.mark.parametrize(
    ("minutes", "method"),
    [(64, "BCT"), (108, "WLT"), (150, "PI"), (215, "Airline")],
)
def test_classify_flight_by_duration(minutes: int, method: str) -> None:
    assert classify_flight("Japan", timedelta(minutes=minutes)) == method  # standard 225


def test_classify_flight_respects_the_plane() -> None:
    # A slow private-island flight (0.84 x standard) is still PI, not an airliner.
    assert classify_flight("Japan", timedelta(minutes=190), "light_aircraft") == "PI"
    assert classify_flight("Japan", timedelta(minutes=190)) == "Airline"
    assert classify_flight("Japan", timedelta(minutes=64), "airliner") == "BCT"


def test_classify_flight_unknown_route() -> None:
    assert classify_flight("Atlantis", timedelta(minutes=60)) is None


_GAP = timedelta(minutes=3)


def test_completed_flight_when_seen_from_take_off_to_landing() -> None:
    last_look = _T0 + timedelta(minutes=148)
    now = last_look + timedelta(minutes=1)
    got = completed_flight(
        "Traveling from Torn to Japan", _T0, last_look, "In Japan", now=now, max_gap=_GAP
    )
    assert got == ("Japan", _T0, last_look + timedelta(seconds=30))


@pytest.mark.parametrize(
    ("prev", "since", "checked_ago", "new"),
    [
        ("Traveling from Torn to Japan", None, 1, "In Japan"),  # take-off not seen
        ("Traveling from Torn to Japan", _T0, 10, "In Japan"),  # landing not seen closely
        ("Traveling from Torn to Japan", _T0, 1, "Traveling from Torn to Japan"),  # still flying
        ("In Japan", _T0, 1, "Traveling from Japan to Torn"),  # took off, not landed
        ("Okay", _T0, 1, "In hospital"),  # not a flight
    ],
)
def test_completed_flight_none_when_not_watched_end_to_end(
    prev: str, since: datetime | None, checked_ago: int, new: str
) -> None:
    now = _T0 + timedelta(hours=3)
    got = completed_flight(
        prev, since, now - timedelta(minutes=checked_ago), new, now=now, max_gap=_GAP
    )
    assert got is None


# --- live_status ----------------------------------------------------------


def test_okay_is_available() -> None:
    assert live_status(PlayerState.OKAY, "Okay", None, None, None).group is StatusGroup.AVAILABLE


def test_hospital_carries_the_exact_release_time() -> None:
    release = _T0 + timedelta(minutes=53)
    status = live_status(PlayerState.HOSPITAL, "In hospital for 53 mins", release, None, None)
    assert status.group is StatusGroup.HOSPITAL
    assert status.release_at == release
    assert status.jailed is False


def test_jail_is_grouped_with_hospital_and_flagged() -> None:
    status = live_status(PlayerState.JAIL, "In jail", _T0, None, None)
    assert status.group is StatusGroup.HOSPITAL
    assert status.jailed is True


def test_flying_out_and_in_and_abroad_are_three_different_groups() -> None:
    out = live_status(PlayerState.TRAVELING, "Traveling from Torn to Japan", None, "airliner", _T0)
    back = live_status(PlayerState.TRAVELING, "Traveling from Japan to Torn", None, "airliner", _T0)
    there = live_status(PlayerState.TRAVELING, "In Japan", None, None, None)
    assert (out.group, back.group, there.group) == (
        StatusGroup.OUTGOING,
        StatusGroup.INCOMING,
        StatusGroup.ABROAD,
    )
    assert out.place == back.place == there.place == "Japan"
    assert out.arrival_at is not None and back.arrival_at is not None
    assert there.arrival_at is None  # landed: nothing to count down to


def test_flight_without_a_seen_take_off_has_no_arrival() -> None:
    status = live_status(
        PlayerState.TRAVELING, "Traveling from Torn to Japan", None, "light_aircraft", None
    )
    assert status.group is StatusGroup.OUTGOING
    assert status.arrival_at is None


def test_unreadable_travel_text_and_unknown_state_go_to_unknown() -> None:
    assert live_status(PlayerState.TRAVELING, "???", None, None, None).group is StatusGroup.UNKNOWN
    assert live_status(PlayerState.UNKNOWN, None, None, None, None).group is StatusGroup.UNKNOWN
