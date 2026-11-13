"""Loads a war's stored activity and summarises it (see :mod:`app.war.activity`)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database.database import Database
from app.database.models import Player
from app.database.repositories import ActivityRepository
from app.utils.time import utcnow
from app.war.activity import ActivitySummary, Member, summarise
from app.war.roster import Roster

# FFScouter's tracker looks back a week too; older habits say little about this war.
DEFAULT_WINDOW = timedelta(days=7)


class ActivityService:
    def __init__(self, database: Database, *, window: timedelta = DEFAULT_WINDOW) -> None:
        self._db = database
        self._window = window

    async def summary(
        self, war_id: int, roster: Roster, *, now: datetime | None = None
    ) -> ActivitySummary:
        now = now or utcnow()
        since = now - self._window
        torn_ids = [e.player_id for e in roster.entries]
        async with self._db.session() as session:
            rows = await session.execute(
                select(Player.id, Player.torn_player_id).where(Player.torn_player_id.in_(torn_ids))
            )
            torn_by_internal = {internal: torn for internal, torn in rows}
            repo = ActivityRepository(session)
            watched = await repo.watch_slots(war_id, since=since)
            slots = await repo.player_slots(list(torn_by_internal), since=since)

        active: dict[int, list[datetime]] = defaultdict(list)
        for internal, slot in slots:
            active[torn_by_internal[internal]].append(slot)
        members = [Member(e.player_id, e.name, e.estimated_bs) for e in roster.entries]
        return summarise(members, active, watched, now=now)
