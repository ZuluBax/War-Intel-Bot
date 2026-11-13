"""Turns a built :class:`~app.war.roster.Roster` into a persisted, ranked
target-priority snapshot (spec §17, Phase 6).

Deliberately reuses whatever roster was already fetched this cycle (Phase 3/5)
rather than re-querying Torn/FFScouter itself — targeting is pure downstream
analysis of data the roster build already paid for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from app.database.database import Database
from app.database.models import TargetRanking, War
from app.database.repositories import (
    ConfirmedStatRepository,
    FlightRecordRepository,
    PlayerRepository,
    TargetRankingRepository,
)
from app.ffscouter.models import FlightWindow
from app.torn.models import PlayerState
from app.utils.logging import get_logger
from app.war.roster import Roster, RosterEntry
from app.war.spy import best_strength
from app.war.targeting import ALGORITHM_VERSION, TargetingWeights, TargetInput, rank

logger = get_logger(__name__)


@dataclass(frozen=True)
class TargetView:
    """A ranked target enriched with roster display fields, for embeds/commands."""

    player_id: int
    name: str | None
    level: int | None
    rank: int
    score: float
    priority: str
    state: PlayerState
    is_available: bool
    # The strength shown and ranked on: a trusted spy report from this server when there
    # is one (``confirmed``), else the FFScouter estimate (app.war.spy.best_strength).
    estimated_bs: float | None = None
    confirmed: bool = False
    fair_fight: float | None = None
    # Full status, for the live board (see app.war.live_status).
    status_description: str | None = None
    status_until: datetime | None = None
    plane_type: str | None = None
    status_since: datetime | None = None  # None = we never saw this status begin
    online_status: str | None = None  # Torn's "Online" / "Idle" / "Offline"
    # FFScouter premium landing window (app.war.flights); None without a premium key.
    flight: FlightWindow | None = None
    # "BCT" / "Airline" if we have seen this player fly an airliner before (their last
    # such flight), so an airliner flight gets one landing window instead of two.
    airliner_method: str | None = None
    # Filled in by app.war.hospital_watch: hospital time the enemy cut with medical
    # items during their current stay, and when they last left hospital early (revive
    # or a big enough item) and with how much time still left.
    med_drops: int = 0
    med_cut: timedelta = timedelta(0)
    out_early_at: datetime | None = None
    out_early_by: timedelta = timedelta(0)


def _target_input(entry: RosterEntry) -> TargetInput:
    return TargetInput(
        player_id=entry.player_id,
        estimated_bs=entry.estimated_bs,
        fair_fight=entry.fair_fight,
        level=entry.level,
        is_available=entry.state is PlayerState.OKAY,
    )


class TargetingService:
    def __init__(self, database: Database, *, weights: TargetingWeights | None = None) -> None:
        self._db = database
        self._weights = weights

    async def refresh(self, war_id: int, roster: Roster) -> list[TargetView]:
        rows: list[TargetRanking] = []
        views: list[TargetView] = []
        async with self._db.session() as session:
            players = PlayerRepository(session)
            internal = {}
            for e in roster.entries:
                player = await players.get_by_torn_id(e.player_id)
                if player is not None:
                    internal[e.player_id] = player
            war = await session.get(War, war_id)
            spies = (
                await ConfirmedStatRepository(session).latest_for_players(
                    war.guild_id, [p.id for p in internal.values()]
                )
                if war is not None
                else {}
            )
            # Rank on the spy total where this server has a trusted one, so a confirmed
            # strong player isn't listed as an easy pick off a stale low estimate.
            confirmed: set[int] = set()
            entries: list[RosterEntry] = []
            for e in roster.entries:
                player = internal.get(e.player_id)
                spy = spies.get(player.id) if player is not None else None
                strength = best_strength(
                    estimate=e.estimated_bs,
                    spy_total=spy.total if spy else None,
                    spy_reported_at=spy.reported_at if spy else None,
                )
                if strength.confirmed:
                    confirmed.add(e.player_id)
                    e = replace(e, estimated_bs=strength.value)
                entries.append(e)
            by_id = {e.player_id: e for e in entries}
            scores = rank([_target_input(e) for e in entries], weights=self._weights)
            airliner = await FlightRecordRepository(session).airliner_methods(
                [p.id for p in internal.values()]
            )
            for i, s in enumerate(scores, start=1):
                entry = by_id.get(s.player_id)
                player = internal.get(s.player_id)
                if player is not None:
                    rows.append(
                        TargetRanking(
                            war_id=war_id,
                            player_id=player.id,
                            rank=i,
                            score=s.score,
                            priority=s.priority,
                            algorithm_version=ALGORITHM_VERSION,
                        )
                    )
                views.append(
                    TargetView(
                        player_id=s.player_id,
                        name=entry.name if entry else None,
                        level=entry.level if entry else None,
                        rank=i,
                        score=s.score,
                        priority=s.priority,
                        state=entry.state if entry else PlayerState.UNKNOWN,
                        is_available=(entry.state is PlayerState.OKAY) if entry else False,
                        estimated_bs=entry.estimated_bs if entry else None,
                        confirmed=s.player_id in confirmed,
                        fair_fight=entry.fair_fight if entry else None,
                        status_description=entry.status_description if entry else None,
                        status_until=entry.status_until if entry else None,
                        plane_type=entry.plane_type if entry else None,
                        status_since=entry.status_since if entry else None,
                        online_status=entry.online_status if entry else None,
                        airliner_method=airliner.get(player.id) if player else None,
                    )
                )
            await TargetRankingRepository(session).replace_for_war(war_id, rows)

        logger.info("targeting.refreshed", war_id=war_id, targets=len(views))
        return views
