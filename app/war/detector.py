"""War detection — turns raw Torn ranked-war data into a state transition.

PHASE 1: defines the state machine and the transition rules. The Torn call that
feeds it lands in Phase 2 (:class:`app.torn.warfare.WarfareService`).

State model (spec §8)::

    NO_WAR       -> no ranked war involving our faction
    WAR_DETECTED -> a ranked war exists but has not started (start in the future)
    WAR_ACTIVE   -> now is between start and end (or end is unknown/None)
    WAR_FINISHED -> end is in the past, or Torn reports a winner

Detection is driven by Torn's own ``start`` / ``end`` / ``winner`` fields, never
by a hard-coded Tuesday 13:00 assumption. The scheduler polls faster around the
expected war window purely to notice a transition sooner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.torn.models import RankedWar
from app.torn.warfare import WarfareService
from app.utils.logging import get_logger
from app.utils.time import utcnow

logger = get_logger(__name__)


class WarState(StrEnum):
    NO_WAR = "NO_WAR"
    WAR_DETECTED = "WAR_DETECTED"
    WAR_ACTIVE = "WAR_ACTIVE"
    WAR_FINISHED = "WAR_FINISHED"


@dataclass(frozen=True)
class WarAssessment:
    state: WarState
    war: RankedWar | None
    our_faction_id: int

    @property
    def enemy_faction_id(self) -> int | None:
        if self.war is None:
            return None
        opponent = self.war.opponent_of(self.our_faction_id)
        return opponent.faction_id if opponent else None


def assess(
    war: RankedWar | None, *, our_faction_id: int, now: datetime | None = None
) -> WarAssessment:
    """Pure function: classify a (possibly absent) ranked war into a state."""
    reference = now or utcnow()

    if war is None or not war.includes(our_faction_id):
        return WarAssessment(WarState.NO_WAR, None, our_faction_id)

    if war.winner_faction_id is not None or (war.end is not None and war.end <= reference):
        return WarAssessment(WarState.WAR_FINISHED, war, our_faction_id)

    if war.start is not None and war.start > reference:
        return WarAssessment(WarState.WAR_DETECTED, war, our_faction_id)

    return WarAssessment(WarState.WAR_ACTIVE, war, our_faction_id)


class WarDetector:
    """Fetches our faction's latest ranked war and classifies it.

    Thin wrapper over :class:`app.torn.warfare.WarfareService` +
    :func:`assess`, so callers (the war manager, the scheduler job) depend on a
    single ``detect()`` coroutine rather than on Torn plumbing.
    """

    def __init__(self, warfare: WarfareService, *, our_faction_id: int) -> None:
        self._warfare = warfare
        self._our_faction_id = our_faction_id

    async def detect(self) -> WarAssessment:
        war = await self._warfare.get_current_ranked_war(self._our_faction_id)
        assessment = assess(war, our_faction_id=self._our_faction_id)
        logger.debug(
            "war.detected",
            state=assessment.state,
            torn_war_id=war.war_id if war else None,
            enemy_faction_id=assessment.enemy_faction_id,
        )
        return assessment

    async def ranked_wars(self) -> list[RankedWar]:
        """Every ranked war Torn lists for our faction, newest first.

        Only needed when a war we have open is not the current one (the bot missed
        its end), to look up how it actually finished.
        """
        return await self._warfare.list_ranked_wars(self._our_faction_id)
