"""When the enemy is busy and when it is quiet — the bot's own version of FFScouter's
premium Activity Tracker.

Unit of activity: a **5-minute slot** in which a player did anything in Torn, read from
Torn's ``last_action`` timestamp (the same unit FFScouter uses). The bot looks at the
enemy every minute from the moment a war is *announced* (Tuesdays ~12:00 TCT, 2-3 days
before it starts), so by the first hit there are days of pattern to go on.

Only slots the bot was actually watching count (``activity_watch``): time the bot was
down is "not watched", never "quiet". Pure functions — no I/O — so easy to test.
Hours are TCT, which is UTC.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SLOT = timedelta(minutes=5)

# An hour needs this many watched slots (30 min) before it can be called busy or quiet.
_MIN_SLOTS_FOR_RANKING = 6
# A player "is usually on" in an hour if they were active in it on at least this share
# of the days that hour was watched.
_USUAL_SHARE = 0.5
# Below this much watching, patterns are shown but flagged as still being learned.
LEARNING_HOURS = 12.0


def activity_slot(when: datetime) -> datetime:
    """The 5-minute slot ``when`` falls in (UTC, floored)."""
    when = when.astimezone(UTC)
    return when.replace(minute=when.minute - when.minute % 5, second=0, microsecond=0)


@dataclass(frozen=True)
class Member:
    player_id: int  # Torn id
    name: str | None
    estimated_bs: float | None = None


@dataclass(frozen=True)
class HourProfile:
    hour: int  # 0-23 TCT
    watched_slots: int
    avg_active: float | None  # enemies active in an average watched slot; None = not watched


@dataclass(frozen=True)
class PlayerPattern:
    member: Member
    usual_hours: tuple[int, ...]  # TCT hours they are usually on, ascending
    active_share: float  # share of watched slots they were active in


@dataclass(frozen=True)
class ActivitySummary:
    member_count: int
    active_now: int
    watched_hours: float
    watched_since: datetime | None
    hours: tuple[HourProfile, ...]  # always 24, hour 0 first
    busiest: tuple[int, ...]
    quietest: tuple[int, ...]
    players: tuple[PlayerPattern, ...]  # strongest first

    @property
    def learning(self) -> bool:
        return self.watched_hours < LEARNING_HOURS


def summarise(
    members: Iterable[Member],
    active_slots: Mapping[int, Iterable[datetime]],
    watched_slots: Iterable[datetime],
    *,
    now: datetime,
    top_players: int = 10,
) -> ActivitySummary:
    """Summarise ``members``' activity over the slots the bot watched.

    ``active_slots`` maps Torn player id -> the slots that player was active in.
    """
    members = list(members)
    watched = set(watched_slots)
    active = {m.player_id: set(active_slots.get(m.player_id, ())) & watched for m in members}

    # Per hour of day: average number of members active per watched slot.
    per_hour_slots: dict[int, list[datetime]] = defaultdict(list)
    for slot in watched:
        per_hour_slots[slot.hour].append(slot)
    count_in_slot: dict[datetime, int] = defaultdict(int)
    for slots in active.values():
        for slot in slots:
            count_in_slot[slot] += 1
    hours = tuple(
        HourProfile(
            hour=h,
            watched_slots=len(per_hour_slots[h]),
            avg_active=(
                sum(count_in_slot[s] for s in per_hour_slots[h]) / len(per_hour_slots[h])
                if per_hour_slots[h]
                else None
            ),
        )
        for h in range(24)
    )
    ranked = sorted(
        (
            p
            for p in hours
            if p.watched_slots >= _MIN_SLOTS_FOR_RANKING and p.avg_active is not None
        ),
        key=lambda p: p.avg_active or 0.0,
    )
    # Busiest and quietest are named only once 6+ hours are rankable, so the two lists
    # never overlap and never rest on one afternoon of data.
    enough = len(ranked) >= 6
    # Listed in clock order, which reads better than rank order in the post.
    quietest = tuple(sorted(p.hour for p in ranked[:3])) if enough else ()
    busiest = tuple(sorted(p.hour for p in ranked[-3:])) if enough else ()

    # "Right now" = active in this slot or the one before (up to ~10 minutes).
    current = activity_slot(now)
    recent = {current, current - SLOT}
    active_now = sum(1 for m in members if recent & set(active_slots.get(m.player_id, ())))

    # Watched (day, hour) pairs, to judge each player's usual hours day by day.
    watched_day_hours: dict[int, set[tuple[datetime, int]]] = defaultdict(set)
    for slot in watched:
        watched_day_hours[slot.hour].add((slot.replace(hour=0, minute=0), slot.hour))

    def pattern(m: Member) -> PlayerPattern:
        on = {(s.replace(hour=0, minute=0), s.hour) for s in active[m.player_id]}
        usual = tuple(
            h
            for h in range(24)
            if watched_day_hours[h]
            and len(on & watched_day_hours[h]) / len(watched_day_hours[h]) >= _USUAL_SHARE
        )
        share = len(active[m.player_id]) / len(watched) if watched else 0.0
        return PlayerPattern(m, usual, share)

    strongest = sorted(members, key=lambda m: (m.estimated_bs is None, -(m.estimated_bs or 0.0)))[
        :top_players
    ]
    ordered = sorted(watched)
    return ActivitySummary(
        member_count=len(members),
        active_now=active_now,
        watched_hours=len(watched) * SLOT.total_seconds() / 3600,
        watched_since=ordered[0] if ordered else None,
        hours=hours,
        busiest=busiest,
        quietest=quietest,
        players=tuple(pattern(m) for m in strongest),
    )


def hour_ranges(hours: Iterable[int]) -> str:
    """``[18, 19, 20, 23, 0, 1]`` -> ``"23-02, 18-21"``: runs of hours as start-end
    (end exclusive, so 18-21 = 18:00 to 21:00), wrapping past midnight."""
    on = sorted(set(hours))
    if not on:
        return ""
    if len(on) == 24:
        return "all day"
    runs: list[list[int]] = []
    for h in on:
        if runs and runs[-1][-1] == h - 1:
            runs[-1].append(h)
        else:
            runs.append([h])
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][-1] == 23:
        runs[0] = runs.pop() + runs[0]  # join the run across midnight
    return ", ".join(f"{r[0]:02d}-{(r[-1] + 1) % 24:02d}" for r in runs)
