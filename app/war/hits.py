"""Keeps ``war_hits`` up to date with our faction's attack feed.

Every attack involving the faction inside the war window is stored, in both
directions (see :class:`app.database.models.WarHit` for why). The sync cursor is
the newest stored attack, so a restart or a long outage resumes exactly where it
left off; a brand-new war starts from its own start time (the whole war so far).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.database.database import Database
from app.database.models import Faction, WarHit
from app.database.repositories import StoredHits, WarHitRepository, WarRepository
from app.torn.attacks import AttackService
from app.torn.models import AttackRecord
from app.utils.logging import get_logger
from app.utils.time import utcnow

logger = get_logger(__name__)

# Re-read this much of the feed behind the cursor. Torn's API can list a hit a
# little after a later one has already appeared, and the API itself caches for
# ~30s; the unique (war, attack id) constraint makes the overlap free of dupes.
_OVERLAP = timedelta(minutes=2)
# Give up backfilling a war after this many passes in a row that filled nothing.
_BACKFILL_PATIENCE = 3


def _to_row(
    record: AttackRecord, *, our_faction_id: int, enemy_faction_id: int | None
) -> WarHit:
    hit_the_enemy = enemy_faction_id is not None and record.defender_faction_id == enemy_faction_id
    return WarHit(
        torn_attack_id=record.attack_id,
        started_at=record.started,
        attacker_id=record.attacker_id,
        attacker_name=record.attacker_name,
        defender_id=record.defender_id,
        defender_name=record.defender_name,
        defender_faction_name=record.defender_faction_name,
        result=record.result[:32],
        respect_gain=record.respect_gain,
        is_war_hit=record.is_ranked_war or hit_the_enemy,
        chain=record.chain or 0,  # 0 = not a chain hit; NULL is reserved for legacy rows
        warlord=record.warlord if record.warlord is not None else 1.0,  # 1.0 = no bonus
        # A stealthed hit *on* us has no attacker faction, so this is False for it.
        is_outgoing=record.attacker_faction_id == our_faction_id,
    )


class WarHitService:
    def __init__(
        self, database: Database, attacks: AttackService, *, our_faction_id: int
    ) -> None:
        self._db = database
        self._attacks = attacks
        self._our_faction_id = our_faction_id
        # The war-detect and refresh jobs can overlap; two syncs racing on the
        # same window would both try to insert the same hits.
        self._lock = asyncio.Lock()
        # Consecutive backfill passes that filled nothing, per war. Stops a row Torn
        # will never return again from pinning every sync to the same old window.
        self._backfill_misses: dict[int, int] = {}

    async def sync(self, war_row_id: int) -> int:
        """Fetch and store new hits for a war; return how many were new."""
        async with self._lock:
            async with self._db.session() as session:
                war = await WarRepository(session).get_by_id(war_row_id)
                latest = await WarHitRepository(session).latest_started(war_row_id)
                incomplete = await WarHitRepository(session).earliest_incomplete(war_row_id)
                enemy = await session.get(Faction, war.enemy_faction_id) if war else None

            if war is None or war.war_start is None or war.war_start > utcnow():
                return 0  # unknown, or matchmade but not started — nothing to fetch yet

            misses = self._backfill_misses.get(war_row_id, 0)
            backfill_from = incomplete if misses < _BACKFILL_PATIENCE else None
            since = war.war_start
            if backfill_from is not None:
                # Hits stored before the chain / warlord columns existed: re-read from the
                # oldest of them so those get filled in, oldest first.
                since = max(since, backfill_from - _OVERLAP)
            elif latest is not None:
                since = max(since, latest - _OVERLAP)

            records = await self._attacks.list_attacks(
                self._our_faction_id, since=since, until=war.war_end
            )

            stored = StoredHits(0, 0)
            if records:
                # Faction id 0 is the placeholder stored when a war's opponent was unknown.
                enemy_id = enemy.torn_faction_id if enemy and enemy.torn_faction_id else None
                rows = [
                    _to_row(r, our_faction_id=self._our_faction_id, enemy_faction_id=enemy_id)
                    for r in records
                ]
                async with self._db.session() as session:
                    stored = await WarHitRepository(session).add_new(war_row_id, rows)

            if backfill_from is not None:
                misses = 0 if stored.filled else misses + 1
                self._backfill_misses[war_row_id] = misses
                if misses >= _BACKFILL_PATIENCE:
                    logger.warning("war.hit_backfill_stalled", war_row_id=war_row_id)
            return stored.new
