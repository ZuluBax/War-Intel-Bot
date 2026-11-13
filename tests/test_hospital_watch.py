"""Medical-cut and revive detection (app.war.hospital_watch) and how the board shows it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord

from app.discord.alert_emoji import AlertEmoji
from app.discord.target_board import target_board_embed
from app.torn.models import PlayerState
from app.war.hospital_watch import OUT_EARLY_SHOWN_FOR, HospitalWatch
from app.war.target_service import TargetView

_T0 = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)
_MIN = timedelta(minutes=1)
_SECOND = timedelta(seconds=1)


def _view(
    pid: int = 1,
    *,
    state: PlayerState = PlayerState.HOSPITAL,
    until: datetime | None = None,
    **extra: object,
) -> TargetView:
    return TargetView(
        player_id=pid,
        name=f"Enemy{pid}",
        level=50,
        rank=pid,
        score=1.0,
        priority="MEDIUM",
        state=state,
        is_available=state is PlayerState.OKAY,
        status_description="In hospital" if state is PlayerState.HOSPITAL else "Okay",
        status_until=until,
        **extra,  # type: ignore[arg-type]
    )


def _step(watch: HospitalWatch, view: TargetView, at: datetime) -> TargetView:
    return watch.observe([view], now=at)[0]


# --- detection -----------------------------------------------------------------------


def test_steady_hospital_stay_raises_nothing() -> None:
    watch = HospitalWatch()
    release = _T0 + timedelta(hours=4, minutes=36)
    for i in range(5):
        seen = _step(watch, _view(until=release), _T0 + i * _MIN)
    assert seen.med_drops == 0
    assert seen.out_early_at is None


def test_one_cut_then_a_second_cut_accumulate() -> None:
    # The user's own example: 4h36m, then 70 minutes off, then another 70.
    watch = HospitalWatch()
    release = _T0 + timedelta(hours=4, minutes=36)
    _step(watch, _view(until=release), _T0)
    first = _step(watch, _view(until=release - 70 * _MIN), _T0 + _MIN)
    assert (first.med_drops, first.med_cut) == (1, 70 * _MIN)
    still = _step(watch, _view(until=release - 70 * _MIN), _T0 + 2 * _MIN)
    assert still.med_drops == 1  # the warning stays for the rest of the stay
    second = _step(watch, _view(until=release - 140 * _MIN), _T0 + 3 * _MIN)
    assert (second.med_drops, second.med_cut) == (2, 140 * _MIN)


def test_tiny_movement_is_ignored() -> None:
    watch = HospitalWatch()
    release = _T0 + timedelta(hours=2)
    _step(watch, _view(until=release), _T0)
    seen = _step(watch, _view(until=release - timedelta(minutes=2)), _T0 + _MIN)
    assert seen.med_drops == 0


def test_later_release_is_a_new_stay_and_resets_the_count() -> None:
    watch = HospitalWatch()
    release = _T0 + timedelta(hours=2)
    _step(watch, _view(until=release), _T0)
    _step(watch, _view(until=release - 70 * _MIN), _T0 + _MIN)
    rehit = _step(watch, _view(until=_T0 + timedelta(hours=3)), _T0 + 2 * _MIN)
    assert rehit.med_drops == 0


def test_no_verdict_across_a_gap_in_observations() -> None:
    # Bot down for 10 minutes: we can't know what happened in between.
    watch = HospitalWatch()
    release = _T0 + timedelta(hours=4)
    _step(watch, _view(until=release), _T0)
    seen = _step(watch, _view(until=release - 120 * _MIN), _T0 + 10 * _MIN)
    assert seen.med_drops == 0


def test_early_exit_is_flagged_then_expires() -> None:
    watch = HospitalWatch()
    _step(watch, _view(until=_T0 + timedelta(hours=3)), _T0)
    out = _step(watch, _view(state=PlayerState.OKAY), _T0 + _MIN)
    assert out.out_early_at == _T0 + _MIN
    assert out.out_early_by == timedelta(hours=3) - _MIN
    later = _step(watch, _view(state=PlayerState.OKAY), _T0 + 2 * _MIN)
    assert later.out_early_at == _T0 + _MIN
    expired = _step(watch, _view(state=PlayerState.OKAY), _T0 + _MIN + OUT_EARLY_SHOWN_FOR)
    assert expired.out_early_at is None


def test_leaving_on_time_is_not_a_revive() -> None:
    watch = HospitalWatch()
    _step(watch, _view(until=_T0 + 30 * _MIN), _T0)
    out = _step(watch, _view(state=PlayerState.OKAY), _T0 + 30 * _MIN + 10 * _SECOND)
    assert out.out_early_at is None


def test_jail_is_not_watched() -> None:
    watch = HospitalWatch()
    _step(watch, _view(state=PlayerState.JAIL, until=_T0 + timedelta(hours=2)), _T0)
    seen = _step(watch, _view(state=PlayerState.JAIL, until=_T0 + 30 * _MIN), _T0 + _MIN)
    assert seen.med_drops == 0
    out = _step(watch, _view(state=PlayerState.OKAY), _T0 + 2 * _MIN)
    assert out.out_early_at is None


def test_players_who_leave_the_roster_are_forgotten() -> None:
    watch = HospitalWatch()
    watch.observe([_view(1, until=_T0 + timedelta(hours=2))], now=_T0)
    watch.observe([], now=_T0 + _MIN)
    assert watch._seen == {}


# --- board -------------------------------------------------------------------------


def _text(embeds: list[discord.Embed]) -> str:
    e = embeds[0]
    return "\n".join([e.description or "", *(f.value or "" for f in e.fields)])


def test_board_shows_warning_then_siren_and_puts_them_first() -> None:
    soon = _T0 + 10 * _MIN
    far = _T0 + timedelta(hours=3)
    plain = _view(1, until=soon)
    once = _view(2, until=far, med_drops=1, med_cut=70 * _MIN)
    twice = _view(3, until=far + _MIN, med_drops=2, med_cut=140 * _MIN)
    text = _text(target_board_embed([plain, once, twice], now=_T0))
    lines = [line for line in text.splitlines() if "Enemy" in line]
    assert "Enemy3" in lines[0] and AlertEmoji.siren in lines[0] and "x2 -2h 20m" in lines[0]
    assert "Enemy2" in lines[1] and AlertEmoji.warning in lines[1] and "-1h 10m" in lines[1]
    assert "Enemy1" in lines[2] and "💊" not in lines[2]


def test_board_flags_early_exits_at_top_of_available() -> None:
    normal = _view(1, state=PlayerState.OKAY)
    healed = _view(2, state=PlayerState.OKAY, out_early_at=_T0 - _MIN, out_early_by=90 * _MIN)
    revived = _view(
        3, state=PlayerState.OKAY, out_early_at=_T0 - _MIN, out_early_by=timedelta(hours=5)
    )
    text = _text(target_board_embed([normal, healed, revived], now=_T0))
    lines = [line for line in text.splitlines() if "Enemy" in line]
    flagged = {line.split("Enemy")[1][0]: line for line in lines[:2]}
    assert "REVIVED" in flagged["3"] and "5h 00m early" in flagged["3"]
    # Under 4h left one big medical item could explain it, so it is not called a revive.
    assert "OUT EARLY" in flagged["2"] and "1h 30m early" in flagged["2"]
    assert all(AlertEmoji.siren in line for line in flagged.values())
    assert "Enemy1" in lines[2] and "EARLY" not in lines[2] and "REVIVED" not in lines[2]
