"""Spots enemies cutting their hospital time with medical items, or being revived.

Torn gives every hospitalised player a fixed release time (``status.until``). It only
moves *earlier* when the player uses a medical item (blood bag, morphine, first aid
kit...), so a drop between two status refreshes means "healing out to attack again".
Leaving hospital while that time is still well in the future means a revive - or a
medical item big enough to take the rest of the time to zero. With more than
``SURELY_REVIVED`` left, no cut seen in real data could do that, so only then is it
called a revive; below it the board says "out early" instead.

Checked against the 15-22 Sep 2026 war before building (``player_status_history``):
92 drops within one stay (largest 3h 46m between two refreshes, bar one 38h oddity),
and 404 early exits back into Torn (253 with 30+ minutes still left). The drop sizes do
NOT map cleanly onto items (120 min was common, but so were 132/140/144 - possibly
perks), so we report minutes cut, never an item name.

Pure in-memory state, one instance per server's war manager. A bot restart forgets
the counts for stays already in progress — the board then simply starts over.
No extra Torn calls: it reads the statuses the roster refresh already fetched.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.torn.models import PlayerState
from app.utils.logging import get_logger
from app.utils.time import utcnow
from app.war.target_service import TargetView

logger = get_logger(__name__)

# Torn's release time is a fixed timestamp, so it never wobbles by itself. The smallest
# drops seen in real data were 5 minutes; anything under this is treated as noise.
_MIN_DROP = timedelta(minutes=3)
# Leaving hospital with less than this left can just be the refresh landing a little
# after the release time, not a revive.
_MIN_EARLY = timedelta(minutes=2)
# Only trust a before/after comparison when the two looks are this close together:
# after a gap (bot down, missed refreshes) we cannot tell what happened in between.
_MAX_GAP = timedelta(minutes=3)
# How long an enemy who left hospital early stays flagged on the board.
OUT_EARLY_SHOWN_FOR = timedelta(minutes=10)
# Above the largest medical cut seen in real data (3h 46m): leaving with more time than
# this left is treated as a revive.
SURELY_REVIVED = timedelta(hours=4)


@dataclass
class _Seen:
    state: PlayerState
    until: datetime | None
    at: datetime
    drops: int = 0
    cut: timedelta = timedelta(0)
    out_early_at: datetime | None = None
    out_early_by: timedelta = timedelta(0)


class HospitalWatch:
    def __init__(self) -> None:
        self._seen: dict[int, _Seen] = {}

    def observe(
        self, targets: list[TargetView], *, now: datetime | None = None
    ) -> list[TargetView]:
        """Compare each enemy with the previous refresh and return ``targets`` with
        ``med_drops`` / ``med_cut`` / ``out_early_at`` / ``out_early_by`` filled in."""
        now = now or utcnow()
        out: list[TargetView] = []
        current: dict[int, _Seen] = {}
        for t in targets:
            seen = self._next(self._seen.get(t.player_id), t, now)
            current[t.player_id] = seen
            if seen.drops:
                t = replace(t, med_drops=seen.drops, med_cut=seen.cut)
            if seen.out_early_at is not None and now - seen.out_early_at < OUT_EARLY_SHOWN_FOR:
                t = replace(t, out_early_at=seen.out_early_at, out_early_by=seen.out_early_by)
            out.append(t)
        # Players who left the enemy faction drop out, so this can't grow across a war.
        self._seen = current
        return out

    def _next(self, prev: _Seen | None, t: TargetView, now: datetime) -> _Seen:
        until = t.status_until if t.state is PlayerState.HOSPITAL else None
        fresh = _Seen(state=t.state, until=until, at=now)
        if prev is None or now - prev.at > _MAX_GAP:
            return fresh

        prev_until = prev.until if prev.state is PlayerState.HOSPITAL else None
        if t.state is PlayerState.HOSPITAL:
            if prev_until is None or until is None:
                return fresh  # a new stay (or Torn sent no time): start counting again
            if prev_until - until >= _MIN_DROP:
                logger.info(
                    "hospital_watch.med_drop",
                    player_id=t.player_id,
                    minutes=round((prev_until - until).total_seconds() / 60),
                    drops=prev.drops + 1,
                )
                return replace(fresh, drops=prev.drops + 1, cut=prev.cut + (prev_until - until))
            if until - prev_until >= _MIN_DROP:
                return fresh  # later release: hit again between refreshes, a new stay
            return replace(fresh, drops=prev.drops, cut=prev.cut)

        if prev_until is not None and prev_until - now >= _MIN_EARLY:
            logger.info(
                "hospital_watch.out_early",
                player_id=t.player_id,
                minutes_left=round((prev_until - now).total_seconds() / 60),
            )
            return replace(fresh, out_early_at=now, out_early_by=prev_until - now)
        # Still out of hospital: keep a recent flag until it expires.
        return replace(fresh, out_early_at=prev.out_early_at, out_early_by=prev.out_early_by)
