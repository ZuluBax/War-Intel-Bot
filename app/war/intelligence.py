"""Estimate history, trend and confidence (spec §12-§13).

``summarise()`` is a pure function over a player's estimate history (newest
first). :class:`IntelligenceService` is the thin DB-backed wrapper the Discord
commands call.

Confidence rubric (documented + configurable — spec §13). It is a heuristic
label, never presented as mathematical certainty:

* HIGH   — newest estimate age <= ``high_max_age`` AND at least
           ``high_min_samples`` historical values AND the recent values agree to
           within ``high_max_spread`` (relative).
* MEDIUM — newest estimate age <= ``medium_max_age`` but the HIGH bar isn't met.
* LOW    — older than ``medium_max_age``, or values vary wildly.
* UNKNOWN — no usable estimate at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.database.repositories import StatEstimateRepository
from app.utils.formatting import format_change, trend_arrow
from app.utils.time import humanise_age, utcnow

_RECENT_WINDOW = 5


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ConfidencePolicy:
    high_max_age: timedelta = timedelta(hours=6)
    medium_max_age: timedelta = timedelta(days=2)
    high_min_samples: int = 3
    high_max_spread: float = 0.10  # 10% relative spread across recent samples


@dataclass(frozen=True)
class EstimatePoint:
    value: float | None
    fair_fight: float | None
    observed_at: datetime
    estimate_timestamp: datetime | None = None

    @property
    def effective_time(self) -> datetime:
        return self.estimate_timestamp or self.observed_at


@dataclass(frozen=True)
class EstimateTrend:
    current: float | None
    previous: float | None
    change_pct_label: str
    arrow: str
    age_label: str
    confidence: Confidence
    sample_count: int

    @property
    def has_estimate(self) -> bool:
        return self.current is not None


def _relative_spread(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    return (max(values) - min(values)) / abs(mean)


def summarise(
    points: Sequence[EstimatePoint],
    *,
    policy: ConfidencePolicy | None = None,
    now: datetime | None = None,
) -> EstimateTrend:
    """Classify a player's estimate history (must be newest-first)."""
    policy = policy or ConfidencePolicy()
    reference = now or utcnow()

    with_value = [p for p in points if p.value is not None]
    if not with_value:
        return EstimateTrend(
            current=None,
            previous=None,
            change_pct_label="—",
            arrow="•",
            age_label="never",
            confidence=Confidence.UNKNOWN,
            sample_count=0,
        )

    newest = with_value[0]
    current = newest.value
    previous = with_value[1].value if len(with_value) > 1 else None
    age = reference - newest.effective_time

    recent_values = [p.value for p in with_value[:_RECENT_WINDOW] if p.value is not None]
    spread = _relative_spread(recent_values)

    if (
        age <= policy.high_max_age
        and len(with_value) >= policy.high_min_samples
        and spread <= policy.high_max_spread
    ):
        confidence = Confidence.HIGH
    elif age <= policy.medium_max_age:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    return EstimateTrend(
        current=current,
        previous=previous,
        change_pct_label=format_change(current, previous),
        arrow=trend_arrow(current, previous),
        age_label=humanise_age(newest.effective_time, now=reference),
        confidence=confidence,
        sample_count=len(with_value),
    )


class IntelligenceService:
    """Derives trend + confidence from persisted StatEstimate history."""

    def __init__(self, policy: ConfidencePolicy | None = None) -> None:
        self._policy = policy or ConfidencePolicy()

    async def summarise_player(
        self, estimates: StatEstimateRepository, player_id: int, *, limit: int = 20
    ) -> EstimateTrend:
        rows = await estimates.history_for_player(player_id, limit=limit)
        points = [
            EstimatePoint(
                value=row.estimated_bs,
                fair_fight=row.fair_fight,
                observed_at=row.observed_at,
                estimate_timestamp=row.estimate_timestamp,
            )
            for row in rows  # repository already returns newest-first
        ]
        return summarise(points, policy=self._policy)
