"""What an enemy can do *right now* — for the live target board.

Pure functions over the status text Torn gives us; no I/O, so the rules are easy to test.

Torn tells us three different things depending on where a player is:

* **Hospital / jail** — an exact release time (``status.until``).
* **Flying** — only the route ("Traveling from Torn to Japan") and the plane type.
  There is **no arrival time**, so the landing time here is an *estimate*: when we
  saw the flight begin, plus that route's flight time for that plane.
* **Abroad** — "In Japan". Nothing to count down to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from app.ffscouter.models import FlightWindow
from app.torn.models import PlayerState

TORN = "torn"

# One-way flight minutes on a standard airliner ticket. These are the figures I recalled
# from Torn's travel page (the wiki blocks automated fetches, so they could not be
# re-read). They were checked against 300+ flights the bot recorded on 15-20 Sep 2026:
# for every one of these 11 destinations the median light-aircraft flight came out at
# 0.654-0.671 of the figure, which would not hold if any base figure were wrong. A single
# flight strays about +/-4% from the median, so an estimate can be a few minutes off.
_STANDARD_MINUTES: dict[str, int] = {
    "mexico": 26,
    "cayman islands": 35,
    "canada": 41,
    "hawaii": 134,
    "united kingdom": 159,
    "argentina": 167,
    "switzerland": 175,
    "japan": 225,
    "china": 242,
    "uae": 271,
    "south africa": 297,
}

# How long each travel method takes: (fastest share, slowest share of _STANDARD_MINUTES,
# plus/minus minutes). Torn makes every flight a little random — a few % either way,
# and a minute or two on even the shortest hop — so a landing is a *window*.
# Fitted on 22 Sep 2026 to 3,672 completed flights (FFScouter premium history for
# our faction and 7 past ranked-war opponents); the fit before this one scored 98.3%
# on 3,202 of these flights it had never seen (FFScouter's own windows: 97.7%).
#   PI       n=2919  98.5% inside, median window 11 min (99% would need 14 min)
#   Airline  n=695   99.1% inside, median 12 min
#   BCT      n=48, WLT n=10: 100% inside; ranges widened a little for the thin samples.
_METHOD_RANGE: dict[str, tuple[float, float, float]] = {
    "Airline": (0.924, 0.976, 1.5),
    "PI": (0.645, 0.692, 1.5),
    "BCT": (0.276, 0.316, 1.0),
    "WLT": (0.458, 0.496, 1.0),
}

# What Torn's ``plane_type`` can mean. An airliner is EITHER a normal ticket OR a
# business-class ticket (BCT, ~3x faster) — Torn does not say which (FFScouter
# confirmed live: both show as "airliner"). light_aircraft = own airstrip (PI).
_PLANE_METHODS: dict[str, tuple[str, ...]] = {
    "light_aircraft": ("PI",),
    "private_jet": ("WLT",),
    "airliner": ("BCT", "Airline"),
}

# We only know the take-off to within one status poll: it happened after the previous
# look, at most this long before we first saw it.
_TAKEOFF_SLACK = timedelta(seconds=60)

_FLYING_RE = re.compile(r"^Traveling from (?P<src>.+?) to (?P<dst>.+?)\s*$", re.IGNORECASE)
_ABROAD_RE = re.compile(r"^In (?P<place>.+?)\s*$", re.IGNORECASE)


class TravelPhase(StrEnum):
    INCOMING = "INCOMING"  # flying from abroad to Torn
    OUTGOING = "OUTGOING"  # flying from Torn to abroad
    ABROAD = "ABROAD"  # landed abroad


@dataclass(frozen=True)
class TravelInfo:
    phase: TravelPhase
    place: str  # the foreign country (never Torn)


def parse_travel(description: str | None) -> TravelInfo | None:
    """Read a status description of a player whose state is TRAVELING.

    Only call this for TRAVELING players: "In hospital ..." also starts with "In ".
    """
    if not description:
        return None
    flying = _FLYING_RE.match(description)
    if flying:
        src, dst = flying["src"], flying["dst"]
        if src.casefold() == TORN:
            return TravelInfo(TravelPhase.OUTGOING, dst)
        if dst.casefold() == TORN:
            return TravelInfo(TravelPhase.INCOMING, src)
        return None  # Torn only allows trips to and from Torn; anything else is unknown
    abroad = _ABROAD_RE.match(description)
    if abroad:
        return TravelInfo(TravelPhase.ABROAD, abroad["place"])
    return None


@dataclass(frozen=True)
class Window:
    """When a flight can land: between ``earliest`` and ``latest``."""

    method: str  # "Airline" | "BCT" | "PI" | "WLT" | FFScouter's label
    earliest: datetime
    latest: datetime


def estimate_windows(
    info: TravelInfo,
    plane_type: str | None,
    departed_at: datetime | None,
    *,
    airliner_method: str | None = None,
) -> tuple[Window, ...]:
    """Landing windows, soonest first — empty when it cannot be worked out honestly.

    Empty if we did not see the take-off, don't know the route or the plane: never a
    guess. An airliner gives two windows (BCT or normal ticket) unless
    ``airliner_method`` says which one this player uses (learned from their past
    flights, see :func:`classify_flight`).
    """
    if info.phase is TravelPhase.ABROAD or departed_at is None or plane_type is None:
        return ()
    base = _STANDARD_MINUTES.get(info.place.casefold())
    methods = _PLANE_METHODS.get(plane_type)
    if base is None or methods is None:
        return ()
    if airliner_method is not None and airliner_method in methods:
        methods = (airliner_method,)
    windows = []
    for method in methods:
        fast, slow, pad = _METHOD_RANGE[method]
        windows.append(
            Window(
                method,
                departed_at - _TAKEOFF_SLACK + timedelta(minutes=base * fast - pad),
                departed_at + timedelta(minutes=base * slow + pad),
            )
        )
    return tuple(sorted(windows, key=lambda w: w.earliest))


def classify_flight(place: str, duration: timedelta, plane_type: str | None = None) -> str | None:
    """Which travel method a finished flight used, from how long it took — the method
    whose range is nearest, among those Torn's ``plane_type`` allows (so a slow
    private-island flight is never mistaken for an airliner). ``None`` for an unknown
    route."""
    base = _STANDARD_MINUTES.get(place.casefold())
    if base is None:
        return None
    share = duration.total_seconds() / 60 / base

    def distance(method: str) -> float:
        fast, slow, _ = _METHOD_RANGE[method]
        return 0.0 if fast <= share <= slow else min(abs(share - fast), abs(share - slow))

    candidates = _PLANE_METHODS.get(plane_type or "", tuple(_METHOD_RANGE))
    return min(candidates, key=distance)


def completed_flight(
    previous_description: str | None,
    previous_since: datetime | None,
    previous_checked_at: datetime | None,
    description: str | None,
    *,
    now: datetime,
    max_gap: timedelta,
) -> tuple[str, datetime, datetime] | None:
    """``(country, took off, landed)`` if the last two looks show a flight we watched
    from take-off to landing; else ``None``.

    The landing is placed halfway between the last "still flying" look and this one.
    """
    if (
        previous_since is None
        or previous_checked_at is None
        or previous_description == description
        or now - previous_checked_at > max_gap
    ):
        return None
    info = parse_travel(previous_description)
    if info is None or info.phase is TravelPhase.ABROAD:
        return None
    landed = previous_checked_at + (now - previous_checked_at) / 2
    return info.place, previous_since, landed


class StatusGroup(IntEnum):
    """Board sections, in the order they are shown."""

    AVAILABLE = 0
    HOSPITAL = 1  # hospital and jail — cannot be attacked until released
    INCOMING = 2
    ABROAD = 3
    OUTGOING = 4
    UNKNOWN = 5


@dataclass(frozen=True)
class LiveStatus:
    group: StatusGroup
    place: str | None = None  # the foreign country, for the three travel groups
    release_at: datetime | None = None  # HOSPITAL: exact, straight from Torn
    # INCOMING / OUTGOING: when it can land, soonest first (two for an airliner whose
    # ticket type we don't know). Empty = landing time unknown.
    windows: tuple[Window, ...] = ()
    from_ffscouter: bool = False  # windows came from FFScouter premium, not our own maths
    jailed: bool = False

    @property
    def arrival_at(self) -> datetime | None:
        return self.windows[0].earliest if self.windows else None


def live_status(
    state: PlayerState,
    description: str | None,
    until: datetime | None,
    plane_type: str | None,
    departed_at: datetime | None,
    flight: FlightWindow | None = None,
    airliner_method: str | None = None,
) -> LiveStatus:
    """Our own landing window when we saw the take-off; otherwise ``flight`` (FFScouter
    premium, which watches all of Torn) fills the gap."""
    if state is PlayerState.OKAY:
        return LiveStatus(StatusGroup.AVAILABLE)
    if state in (PlayerState.HOSPITAL, PlayerState.JAIL):
        return LiveStatus(
            StatusGroup.HOSPITAL, release_at=until, jailed=state is PlayerState.JAIL
        )
    if state is PlayerState.TRAVELING:
        info = parse_travel(description)
        if info is None:
            return LiveStatus(StatusGroup.UNKNOWN)
        group = {
            TravelPhase.INCOMING: StatusGroup.INCOMING,
            TravelPhase.OUTGOING: StatusGroup.OUTGOING,
            TravelPhase.ABROAD: StatusGroup.ABROAD,
        }[info.phase]
        windows = estimate_windows(info, plane_type, departed_at, airliner_method=airliner_method)
        if not windows and flight is not None and info.phase is not TravelPhase.ABROAD:
            ffs = Window(
                flight.travel_method or "Unknown", flight.earliest_arrival, flight.latest_arrival
            )
            return LiveStatus(group, place=info.place, windows=(ffs,), from_ffscouter=True)
        return LiveStatus(group, place=info.place, windows=windows)
    return LiveStatus(StatusGroup.UNKNOWN)
