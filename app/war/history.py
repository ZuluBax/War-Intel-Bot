"""Completed-war history lookup (spec §22, Phase 7).

Finished-war detection, result capture, and retention of historic
roster/estimate rows all already happen as a side effect of the normal war
lifecycle — ``WarManager._reconcile`` sets ``war_end``/``result`` the moment a
war's state reaches ``WAR_FINISHED`` (Phase 2), and nothing anywhere deletes a
``Player`` / ``WarPlayer`` / ``StatEstimate`` row once written (Phase 3). There
is no separate "finalise" step to run — this module is purely the read side:
listing a server's past wars for ``/history``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.database.database import Database
from app.database.repositories import WarRepository


@dataclass(frozen=True)
class HistoricWar:
    torn_war_id: int | None
    enemy_faction_name: str | None
    result: str | None  # WIN / LOSS / DRAW / None
    war_start: datetime | None
    war_end: datetime | None


class WarHistoryService:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def recent(self, guild_id: int, *, limit: int = 10) -> list[HistoricWar]:
        async with self._db.session() as session:
            wars = await WarRepository(session).list_history(guild_id, limit=limit)
        return [
            HistoricWar(
                torn_war_id=w.torn_war_id,
                enemy_faction_name=w.enemy_faction_name,
                result=w.result,
                war_start=w.war_start,
                war_end=w.war_end,
            )
            for w in wars
        ]
