"""Enemy activity: slots, the summary maths, recording during roster builds, the post."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select

from app.database.database import Database
from app.database.models import ActivityWatch, PlayerActivity
from app.discord.activity_board import activity_embed
from app.ffscouter.client import FFScouterClient
from app.torn.faction import FactionService
from app.torn.models import FactionMember, FactionSummary, PlayerState, TornStatus
from app.war.activity import Member, activity_slot, hour_ranges, summarise
from app.war.roster import EnemyRosterService

_DAY = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)


def _slots(start: datetime, hours: float) -> list[datetime]:
    return [start + timedelta(minutes=5 * i) for i in range(int(hours * 12))]


# --- activity_slot / hour_ranges -----------------------------------------------------


def test_activity_slot_floors_to_five_minutes() -> None:
    assert activity_slot(datetime(2026, 9, 29, 18, 7, 59, tzinfo=UTC)) == datetime(
        2026, 9, 29, 18, 5, tzinfo=UTC
    )


def test_hour_ranges_joins_runs_and_wraps_midnight() -> None:
    assert hour_ranges([18, 19, 20, 23, 0, 1]) == "23-02, 18-21"
    assert hour_ranges([5]) == "05-06"
    assert hour_ranges([]) == ""
    assert hour_ranges(range(24)) == "all day"


# --- summarise -------------------------------------------------------------------------


def test_busy_evening_quiet_morning() -> None:
    watched = _slots(_DAY, 48)  # two full days watched
    members = [Member(i, f"E{i}", float(i)) for i in range(1, 11)]
    active: dict[int, list[datetime]] = {}
    for m in members:
        slots = []
        for day in (0, 1):
            base = _DAY + timedelta(days=day)
            slots += _slots(base + timedelta(hours=19), 3)  # everyone on 19:00-22:00
        if m.player_id <= 2:
            slots += [_DAY + timedelta(hours=5, minutes=5)]  # two early birds, once
        active[m.player_id] = slots

    s = summarise(members, active, watched, now=_DAY + timedelta(days=2))
    assert s.watched_hours == 48
    assert not s.learning
    assert set(s.busiest) == {19, 20, 21}
    assert 5 not in s.quietest and len(s.quietest) == 3
    assert s.hours[20].avg_active == 10.0
    assert s.hours[3].avg_active == 0.0
    # Strongest first, and each is "usually on" 19-22.
    assert [p.member.player_id for p in s.players][:3] == [10, 9, 8]
    assert hour_ranges(s.players[0].usual_hours) == "19-22"


def test_unwatched_hours_are_not_called_quiet() -> None:
    watched = _slots(_DAY + timedelta(hours=12), 10)  # only 12:00-22:00 watched
    members = [Member(1, "A")]
    s = summarise(members, {1: watched[:12]}, watched, now=_DAY + timedelta(hours=22))
    assert s.hours[3].avg_active is None  # never watched: unknown, not zero
    assert all(h in range(12, 22) for h in s.quietest)
    assert s.learning  # under 12 hours watched


def test_activity_outside_watched_slots_is_ignored() -> None:
    watched = _slots(_DAY + timedelta(hours=12), 1)
    s = summarise([Member(1, "A")], {1: [_DAY + timedelta(hours=3)]}, watched, now=_DAY)
    assert all(h.avg_active in (None, 0.0) for h in s.hours)


def test_active_now_counts_the_last_ten_minutes() -> None:
    now = _DAY + timedelta(hours=18, minutes=7)
    active = {
        1: [activity_slot(now)],
        2: [activity_slot(now) - timedelta(minutes=5)],
        3: [activity_slot(now) - timedelta(minutes=15)],  # too long ago
    }
    s = summarise([Member(i, str(i)) for i in (1, 2, 3)], active, _slots(_DAY, 24), now=now)
    assert s.active_now == 2


def test_no_busiest_or_quietest_on_too_little_data() -> None:
    watched = _slots(_DAY + timedelta(hours=10), 3)  # 3 hours
    s = summarise([Member(1, "A")], {1: watched}, watched, now=_DAY + timedelta(hours=13))
    assert s.busiest == () and s.quietest == ()


# --- the post --------------------------------------------------------------------------


def test_embed_shows_chart_busy_quiet_and_players() -> None:
    watched = _slots(_DAY, 48)
    members = [Member(1, "Big_Hitter", 4.2e9), Member(2, "Small", 1e6)]
    evening = [s for s in watched if 19 <= s.hour < 22]
    s = summarise(members, {1: evening, 2: evening}, watched, now=_DAY + timedelta(hours=44))
    embed = activity_embed(s, now=_DAY + timedelta(hours=44))
    text = embed.description or ""
    assert "Active right now:" in text
    assert "🔥 **Busiest:** 19:00, 20:00, 21:00" in text
    assert "😴 **Quietest:**" in text
    assert "```" in text and "◀ now" in text
    assert text.count("\n") > 24  # one chart line per hour
    field = embed.fields[0].value or ""
    assert "Big\\_Hitter" in field and "19-22" in field
    assert len(text) <= 4096


def test_embed_while_still_learning() -> None:
    watched = _slots(_DAY + timedelta(hours=12), 2)
    s = summarise([Member(1, "A")], {}, watched, now=_DAY + timedelta(hours=14))
    text = activity_embed(s, now=_DAY + timedelta(hours=14)).description or ""
    assert "Still learning" in text
    assert "not watched yet" in text
    assert "Busiest" not in text


# --- recording during roster builds ----------------------------------------------------


class _StubFaction:
    def __init__(self, members: list[FactionMember]) -> None:
        self.members = members

    async def get_members(self, faction_id: int) -> FactionSummary:
        return FactionSummary(faction_id=faction_id, members=tuple(self.members))


def _member(pid: int, last_action: datetime | None) -> FactionMember:
    return FactionMember(
        player_id=pid,
        name=f"E{pid}",
        level=40,
        status=TornStatus(state=PlayerState.OKAY, description="Okay"),
        last_action=last_action,
    )


async def _count(database: Database, model: type) -> int:
    async with database.session() as session:
        return int((await session.scalar(select(func.count()).select_from(model))) or 0)


async def test_build_records_activity_slots_once_and_a_watch_slot(database: Database) -> None:
    acted = datetime(2026, 9, 29, 18, 7, tzinfo=UTC)
    faction = _StubFaction([_member(1, acted), _member(2, None)])
    svc = EnemyRosterService(database, cast(FactionService, faction), cast(FFScouterClient, None))
    await svc.build(1, 777, fetch_estimates=False)
    await svc.build(1, 777, fetch_estimates=False)  # same last action: nothing new
    assert await _count(database, PlayerActivity) == 1
    assert await _count(database, ActivityWatch) >= 1

    faction.members = [_member(1, acted + timedelta(minutes=6)), _member(2, None)]
    await svc.build(1, 777, fetch_estimates=False)
    assert await _count(database, PlayerActivity) == 2  # a new slot


async def test_activity_survives_a_restart_without_duplicates(database: Database) -> None:
    acted = datetime(2026, 9, 29, 18, 7, tzinfo=UTC)
    faction = _StubFaction([_member(1, acted)])
    for _ in range(2):  # a fresh service = the bot restarted (in-memory cache empty)
        svc = EnemyRosterService(
            database, cast(FactionService, faction), cast(FFScouterClient, None)
        )
        await svc.build(1, 777, fetch_estimates=False)
    assert await _count(database, PlayerActivity) == 1
