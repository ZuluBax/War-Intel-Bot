"""Typed value objects for FFScouter responses.

Field names mirror FFScouter's ``/get-stats`` response (verified — see
docs/api-contracts.md). Every field is optional because FFScouter returns a row
per requested target even when it has no estimate for that player, and premium-
only fields are absent for a free key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FFScouterStat:
    player_id: int
    fair_fight: float | None = None
    bs_estimate: float | None = None
    bs_estimate_human: str | None = None
    bss_public: float | None = None
    # FFScouter's own "when this estimate was last refreshed" (UTC).
    last_updated: datetime | None = None
    # "bss" | "premium" | "spies" | None
    source: str | None = None
    premium_insights_available: bool = False

    @property
    def has_estimate(self) -> bool:
        return self.bs_estimate is not None


@dataclass(frozen=True)
class FlightWindow:
    """FFScouter's (premium) estimate of when a player in the air will land.

    FFScouter watches take-offs across Torn, so it usually has this even when the bot
    itself never saw the flight begin. ``earliest``/``latest`` bound the landing; the
    gap is FFScouter's own uncertainty (e.g. whether a travel book is in use).
    """

    takeoff_at: datetime
    earliest_arrival: datetime
    latest_arrival: datetime
    # "PI" | "Airline" | "WLT" | "BCT" | "Unknown" | None
    travel_method: str | None = None
    book_likely: bool | None = None
