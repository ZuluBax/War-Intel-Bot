from __future__ import annotations

import pytest

from app.utils.formatting import (
    format_change,
    format_stat,
    sanitise_channel_name,
    trend_arrow,
    truncate,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "?"),
        (0, "0"),
        (950, "950"),
        (2_840_000_000, "2.84B"),
        (1_500_000, "1.50M"),
        (3_200_000_000_000, "3.20T"),
    ],
)
def test_format_stat(value: float | None, expected: str) -> None:
    assert format_stat(value) == expected


def test_format_change() -> None:
    assert format_change(2.84, 2.81) == "+1.07%"
    assert format_change(2.70, 2.80) == "-3.57%"
    assert format_change(1.0, None) == "—"
    assert format_change(1.0, 0) == "—"


def test_trend_arrow_deadband() -> None:
    assert trend_arrow(100.4, 100) == "▬"  # +0.4% is within the 0.5% deadband
    assert trend_arrow(101, 100) == "▲"  # +1.0% clears the deadband
    assert trend_arrow(110, 100) == "▲"
    assert trend_arrow(90, 100) == "▼"
    assert trend_arrow(None, 100) == "•"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The Black Hand", "the-black-hand"),
        ("  Spaces   &&&  dashes  ", "spaces-dashes"),
        ("!!!", "faction"),
        ("Ünïcödé Crew", "n-c-d-crew"),
    ],
)
def test_sanitise_channel_name(raw: str, expected: str) -> None:
    assert sanitise_channel_name(raw) == expected


def test_truncate() -> None:
    assert truncate("hello", 10) == "hello"
    assert truncate("hello world", 5) == "hell…"
