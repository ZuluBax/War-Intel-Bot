from __future__ import annotations

from datetime import timedelta

from app.utils.time import utcnow
from app.war.intelligence import Confidence, EstimatePoint, summarise


def _point(value: float | None, *, age: timedelta) -> EstimatePoint:
    ts = utcnow() - age
    return EstimatePoint(value=value, fair_fight=1.5, observed_at=ts, estimate_timestamp=ts)


def test_no_points_is_unknown() -> None:
    trend = summarise([])
    assert trend.confidence is Confidence.UNKNOWN
    assert trend.has_estimate is False
    assert trend.age_label == "never"
    assert trend.arrow == "•"


def test_single_recent_point_is_medium() -> None:
    # Recent, but only one sample -> cannot clear the HIGH bar (needs >= 3).
    trend = summarise([_point(2.8e9, age=timedelta(minutes=10))])
    assert trend.confidence is Confidence.MEDIUM
    assert trend.current == 2.8e9
    assert trend.previous is None
    assert trend.sample_count == 1


def test_several_recent_consistent_points_are_high() -> None:
    # Rising ~1.8% step, total spread ~2.4% -> well inside the 10% HIGH bar.
    points = [
        _point(2.90e9, age=timedelta(hours=1)),
        _point(2.85e9, age=timedelta(hours=3)),
        _point(2.83e9, age=timedelta(hours=5)),
    ]
    trend = summarise(points)
    assert trend.confidence is Confidence.HIGH
    assert trend.current == 2.90e9
    assert trend.previous == 2.85e9
    assert trend.arrow == "▲"
    assert trend.change_pct_label.startswith("+")


def test_recent_but_wildly_varying_points_are_medium_not_high() -> None:
    points = [
        _point(4.0e9, age=timedelta(hours=1)),
        _point(2.0e9, age=timedelta(hours=2)),
        _point(3.0e9, age=timedelta(hours=3)),
    ]
    trend = summarise(points)
    assert trend.confidence is Confidence.MEDIUM  # spread too wide for HIGH


def test_stale_newest_estimate_is_low() -> None:
    points = [
        _point(2.8e9, age=timedelta(days=5)),
        _point(2.7e9, age=timedelta(days=6)),
        _point(2.6e9, age=timedelta(days=7)),
    ]
    trend = summarise(points)
    assert trend.confidence is Confidence.LOW
    assert "d ago" in trend.age_label


def test_points_without_value_are_ignored() -> None:
    points = [
        _point(None, age=timedelta(minutes=5)),
        _point(2.5e9, age=timedelta(hours=2)),
    ]
    trend = summarise(points)
    assert trend.current == 2.5e9
    assert trend.sample_count == 1
