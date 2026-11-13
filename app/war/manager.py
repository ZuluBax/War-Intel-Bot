"""War lifecycle orchestrator — the top-level coordinator (spec §8, §30).

``tick()`` runs one cycle (called at ``war_check_interval_seconds``):

1. detect the current ranked war state
2. reconcile the ``wars`` table **idempotently** (unique ``torn_war_id``) —
   register a new war once, or apply state / result / timing changes to a known
   one, writing a ``WarLogEvent`` for anything log-worthy (spec §32)
3. when a *new, not-yet-finished* war is registered, build the enemy roster
   (Phase 3) and the Discord workspace (Phase 4)

``refresh_active()`` (Phase 5) is the periodic re-fetch for an already-running
war, called by the scheduler at ``status_refresh_interval_seconds`` (cheap,
Torn-only) and ``stats_refresh_interval_seconds`` (full, includes FFScouter),
and on demand by ``/refresh``. It diffs the enemy roster for joins/leaves,
writes those as log events too, and re-publishes the live embeds in place.

Every roster build/refresh also recomputes the target-priority snapshot
(Phase 6, ``app.war.target_service.TargetingService``) from the same roster,
so ``#target-board`` never falls out of sync with ``#enemy-stats``.

Each refresh also pulls our faction's new attacks into ``war_hits`` and the war log
posts them (war hits green, outside hits orange) — see ``app.war.hits``.

A war still open that is not Torn's current one (the bot missed its end) is closed
from Torn's ranked-war list on the next war check — see ``_close_stale_wars``.

The manager composes services; it contains no HTTP or Discord code itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.database.database import Database
from app.database.models import War
from app.database.repositories import FactionRepository, WarLogRepository, WarRepository
from app.utils.logging import get_logger
from app.utils.time import utcnow
from app.war.activity import ActivitySummary
from app.war.activity_service import ActivityService
from app.war.detector import WarAssessment, WarDetector, WarState, assess
from app.war.flights import FlightTracker
from app.war.hits import WarHitService
from app.war.hospital_watch import HospitalWatch
from app.war.roster import EnemyRosterService, Roster
from app.war.score import WarScore, score_from_war
from app.war.target_service import TargetingService, TargetView
from app.war.warlog import (
    roster_join_event,
    roster_left_event,
    state_change_event,
    war_finished_event,
    war_registered_event,
    war_summary_event,
)

logger = get_logger(__name__)


class WarPublisher(Protocol):
    """Renders a war + roster to wherever it needs to go (Discord). Kept as a
    protocol so the manager stays free of Discord imports."""

    async def publish_war(self, war_row_id: int, roster: Roster) -> None: ...

    async def publish_targets(self, war_row_id: int, targets: list[TargetView]) -> None:
        """Render the target-priority board (spec §17)."""
        ...

    async def clear_targets(self, war_row_id: int) -> None:
        """Blank the target-priority board once the war ends."""
        ...

    async def publish_log(self, war_row_id: int) -> None:
        """Post any not-yet-posted war-log events (spec §32)."""
        ...

    async def publish_score(self, war_row_id: int, score: WarScore) -> bool:
        """Edit the live-score box; True once it is on Discord, False if deferred."""
        ...

    async def publish_activity(self, war_row_id: int, summary: ActivitySummary) -> None:
        """Render when the enemy is busy / quiet."""
        ...


def _result_for(winner_faction_id: int | None, our_torn_faction_id: int) -> str | None:
    if winner_faction_id is None:
        return None
    return "WIN" if winner_faction_id == our_torn_faction_id else "LOSS"


@dataclass(frozen=True)
class _Reconciled:
    """What ``_reconcile`` changed this tick, for ``tick()`` to act on."""

    war_row_id: int
    enemy_torn_faction_id: int | None
    enemy_faction_name: str | None
    is_new: bool  # True the first time this Torn war is seen
    is_active: bool  # False if finished (either just now or already, on first sighting)
    log_event_added: bool  # a WarLogEvent row was written this call


class WarManager:
    def __init__(
        self,
        database: Database,
        detector: WarDetector,
        *,
        guild_id: int,
        our_faction_id: int,
        roster_service: EnemyRosterService | None = None,
        targeting_service: TargetingService | None = None,
        hit_service: WarHitService | None = None,
        publisher: WarPublisher | None = None,
        flight_tracker: FlightTracker | None = None,
        activity_service: ActivityService | None = None,
        hospital_watch: HospitalWatch | None = None,
    ) -> None:
        self._db = database
        self._detector = detector
        self._guild_id = guild_id
        self._our_faction_id = our_faction_id
        self._roster_service = roster_service
        self._targeting_service = targeting_service
        self._hit_service = hit_service
        self._publisher = publisher
        self._flight_tracker = flight_tracker
        self._activity_service = activity_service
        self._hospital_watch = hospital_watch
        # Wars whose hit feed is currently failing, so a persistent problem (e.g. a
        # key that cannot read attacks) is logged once, not every minute.
        self._hit_sync_warned: set[int] = set()
        # Wars that have ended but whose final score is not yet on Discord.
        self._final_score_pending: set[int] = set()

    async def tick(self) -> WarAssessment:
        """One war-detection cycle for this server. Called by the runner."""
        assessment = await self._detector.detect()

        if assessment.state is WarState.NO_WAR or assessment.war is None:
            await self._close_stale_wars(current_torn_war_id=None)
            return assessment

        reconciled = await self._reconcile(assessment)
        if reconciled is not None:
            if reconciled.is_new and reconciled.is_active:
                await self._build_roster(reconciled)
            elif reconciled.log_event_added:
                if not reconciled.is_active and not reconciled.is_new:
                    # The war just ended: sweep up the hits since the last refresh,
                    # then blank the target board — its last-known attack-now list is
                    # now stale and would otherwise sit there until the next war.
                    await self._sync_hits(reconciled.war_row_id)
                    await self._clear_targets(reconciled.war_row_id)
                await self._publish_log(reconciled.war_row_id)
        await self._close_stale_wars(current_torn_war_id=assessment.war.war_id)
        # After the branches above so a brand-new war's workspace exists first. Runs
        # even when nothing was reconciled — the score moves without the state changing.
        await self._publish_score(assessment, reconciled)
        return assessment

    async def refresh_active(self, *, include_stats: bool = True) -> Roster | None:
        """Periodic re-fetch for an already-registered, still-open war (Phase 5).

        ``include_stats=False`` is the cheap status-only pass (Torn faction
        members only, no FFScouter call) run at ``status_refresh_interval_seconds``;
        ``include_stats=True`` is the fuller pass at ``stats_refresh_interval_seconds``.
        Either way, roster join/leave since the last refresh become war-log events
        and the live embeds are re-published in place.
        """
        if self._roster_service is None:
            return None
        async with self._db.session() as session:
            war = await WarRepository(session).get_active(self._guild_id)
        if war is None:
            return None

        await self._sync_hits(war.id)  # before the roster: a roster failure returns early below
        try:
            diff = await self._roster_service.refresh_with_diff(
                war.id, fetch_estimates=include_stats
            )
        except Exception as exc:  # a roster failure must never kill the refresh cycle
            logger.error(
                "war.refresh_failed",
                war_row_id=war.id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            return None

        if diff.joined or diff.left:
            async with self._db.session() as session:
                logs = WarLogRepository(session)
                for pid, name in diff.joined:
                    logs.add(roster_join_event(war.id, player_id=pid, name=name))
                for pid, name in diff.left:
                    logs.add(roster_left_event(war.id, player_id=pid, name=name))

        if self._publisher is not None:
            try:
                await self._publisher.publish_war(war.id, diff.roster)
            except Exception as exc:  # a Discord failure must never kill the refresh cycle
                logger.error(
                    "war.publish_failed",
                    war_row_id=war.id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
        await self._refresh_targets(war.id, diff.roster)
        if include_stats:  # the 15-minute pass: an hourly pattern needs no fresher redraw
            await self._refresh_activity(war.id, diff.roster)
        await self._publish_log(war.id)
        return diff.roster

    async def _build_roster(self, reconciled: _Reconciled) -> None:
        if self._roster_service is None or reconciled.enemy_torn_faction_id is None:
            return
        try:
            roster = await self._roster_service.build(
                reconciled.war_row_id,
                reconciled.enemy_torn_faction_id,
                enemy_faction_name=reconciled.enemy_faction_name,
            )
            logger.info(
                "war.roster_built",
                war_row_id=reconciled.war_row_id,
                players=len(roster.entries),
                estimates_available=roster.estimates_available,
            )
        except Exception as exc:  # a roster failure must never kill the detection tick
            logger.error(
                "war.roster_build_failed",
                war_row_id=reconciled.war_row_id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            return

        if self._publisher is not None:
            try:
                await self._publisher.publish_war(reconciled.war_row_id, roster)
            except Exception as exc:  # a Discord failure must never kill the tick
                logger.error(
                    "war.publish_failed",
                    war_row_id=reconciled.war_row_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
        await self._refresh_targets(reconciled.war_row_id, roster)
        await self._refresh_activity(reconciled.war_row_id, roster)
        await self._publish_log(reconciled.war_row_id)

    async def _refresh_activity(self, war_row_id: int, roster: Roster) -> None:
        if self._activity_service is None or self._publisher is None:
            return
        try:
            summary = await self._activity_service.summary(war_row_id, roster)
            await self._publisher.publish_activity(war_row_id, summary)
        except Exception as exc:  # the activity post is extra; never kill the tick
            logger.error(
                "war.activity_failed",
                war_row_id=war_row_id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )

    async def _refresh_targets(self, war_row_id: int, roster: Roster) -> None:
        if self._targeting_service is None:
            return
        try:
            targets = await self._targeting_service.refresh(war_row_id, roster)
        except Exception as exc:  # a targeting failure must never kill the tick
            logger.error(
                "war.targeting_failed",
                war_row_id=war_row_id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            return
        if self._flight_tracker is not None:
            try:
                targets = await self._flight_tracker.enrich(targets)
            except Exception as exc:  # landing windows are optional; publish without them
                logger.error(
                    "war.flight_enrich_failed",
                    war_row_id=war_row_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
        if self._hospital_watch is not None:
            try:
                targets = self._hospital_watch.observe(targets)
            except Exception as exc:  # med/revive warnings are extra; publish without them
                logger.error(
                    "war.hospital_watch_failed",
                    war_row_id=war_row_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
        if self._publisher is not None:
            try:
                await self._publisher.publish_targets(war_row_id, targets)
            except Exception as exc:  # a Discord failure must never kill the tick
                logger.error(
                    "war.publish_targets_failed",
                    war_row_id=war_row_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )

    async def _clear_targets(self, war_row_id: int) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher.clear_targets(war_row_id)
        except Exception as exc:  # a Discord failure must never kill the tick
            logger.error(
                "war.clear_targets_failed",
                war_row_id=war_row_id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )

    async def _sync_hits(self, war_row_id: int) -> None:
        if self._hit_service is None:
            return
        try:
            added = await self._hit_service.sync(war_row_id)
        except Exception as exc:  # the hit feed is best-effort; it must never kill a tick
            if war_row_id not in self._hit_sync_warned:
                self._hit_sync_warned.add(war_row_id)
                logger.warning(
                    "war.hit_sync_failed",
                    war_row_id=war_row_id,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
            return
        self._hit_sync_warned.discard(war_row_id)
        if added:
            logger.info("war.hits_synced", war_row_id=war_row_id, new_hits=added)

    async def _publish_score(
        self, assessment: WarAssessment, reconciled: _Reconciled | None
    ) -> None:
        """Refresh the live-score box from this tick's ``/rankedwars`` data — no extra
        Torn call. Live while the war runs; the final score is posted once it ends."""
        war = assessment.war
        if self._publisher is None or war is None:
            return
        finished = assessment.state is WarState.WAR_FINISHED
        if not finished and assessment.state is not WarState.WAR_ACTIVE:
            return  # not started yet: 0 - 0 adds nothing

        async with self._db.session() as session:
            row = await WarRepository(session).get_by_torn_war_id(self._guild_id, war.war_id)
        if row is None:
            return

        if finished:
            # Only a war we watched end gets a final score; one first seen already
            # finished never had a workspace. Kept pending until Discord accepts it, so a
            # failed post cannot leave a stale "live" score standing as the final one.
            if reconciled is not None and not reconciled.is_new:
                self._final_score_pending.add(row.id)
            if row.id not in self._final_score_pending:
                return

        score = score_from_war(
            war,
            self._our_faction_id,
            finished=finished,
            result=_result_for(war.winner_faction_id, self._our_faction_id),
        )
        if score is None:
            return
        try:
            posted = await self._publisher.publish_score(row.id, score)
        except Exception as exc:  # a Discord failure must never kill the tick
            logger.error(
                "war.publish_score_failed",
                war_row_id=row.id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            return
        if finished and posted:
            self._final_score_pending.discard(row.id)

    async def _publish_log(self, war_row_id: int) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher.publish_log(war_row_id)
        except Exception as exc:  # a Discord failure must never kill the tick
            logger.error(
                "war.publish_log_failed",
                war_row_id=war_row_id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )

    async def _close_stale_wars(self, *, current_torn_war_id: int | None) -> None:
        """Close any war we still have open that is not Torn's current one.

        The detector only follows Torn's newest war, so if the bot was down when a war
        ended and the next one appeared before it came back, the old row would stay
        open forever. Look the old war up in Torn's list and close it the same way as
        a war we watched end. Never closes on missing data: a Torn error or an empty
        list leaves it open (a glitch is likelier than a real end).
        """
        async with self._db.session() as session:
            stale = [
                w
                for w in await WarRepository(session).open_wars(self._guild_id)
                if w.torn_war_id != current_torn_war_id
            ]
        if not stale:
            return

        try:
            torn_wars = {w.war_id: w for w in await self._detector.ranked_wars()}
        except Exception as exc:  # retried next tick; must never kill the war check
            logger.warning(
                "war.stale_check_failed",
                stale_torn_war_ids=[w.torn_war_id for w in stale],
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            return
        if not torn_wars:
            logger.warning(
                "war.stale_war_kept",
                stale_torn_war_ids=[w.torn_war_id for w in stale],
                reason="Torn listed no ranked wars",
            )
            return

        for row in stale:
            # A row with no Torn id can never be matched, so it is closed as unlisted.
            torn_war = torn_wars.get(row.torn_war_id) if row.torn_war_id is not None else None
            if torn_war is None:
                reconciled = await self._close_unlisted(row.id)
            else:
                old = assess(torn_war, our_faction_id=self._our_faction_id)
                if old.state is not WarState.WAR_FINISHED:
                    logger.info(
                        "war.stale_war_kept",
                        torn_war_id=row.torn_war_id,
                        reason=f"Torn still reports it as {old.state.value}",
                    )
                    continue
                reconciled = await self._reconcile(old)  # result, end time, log events
            if reconciled is None or not reconciled.log_event_added:
                continue
            logger.info(
                "war.stale_war_closed",
                torn_war_id=row.torn_war_id,
                listed_by_torn=torn_war is not None,
            )
            # The same wrap-up as a war we watched end (see tick).
            await self._sync_hits(reconciled.war_row_id)
            await self._clear_targets(reconciled.war_row_id)
            await self._publish_log(reconciled.war_row_id)

    async def _close_unlisted(self, war_row_id: int) -> _Reconciled | None:
        """Finish a war Torn no longer lists at all — result unknown."""
        async with self._db.session() as session:
            war = await session.get(War, war_row_id)
            if war is None or war.status not in ("WAR_DETECTED", "WAR_ACTIVE"):
                return None
            war.status = WarState.WAR_FINISHED.value
            if war.war_end is None:
                war.war_end = utcnow()
            logs = WarLogRepository(session)
            logs.add(war_finished_event(war.id, result=war.result))
            logs.add(war_summary_event(war.id))
        return _Reconciled(
            war_row_id=war_row_id,
            enemy_torn_faction_id=None,
            enemy_faction_name=None,
            is_new=False,
            is_active=False,
            log_event_added=True,
        )

    async def _reconcile(self, assessment: WarAssessment) -> _Reconciled | None:
        war = assessment.war
        assert war is not None
        opponent = war.opponent_of(self._our_faction_id)
        us = next((f for f in war.factions if f.faction_id == self._our_faction_id), None)

        async with self._db.session() as session:
            wars = WarRepository(session)
            factions = FactionRepository(session)

            existing = await wars.get_by_torn_war_id(self._guild_id, war.war_id)

            our_row = await factions.upsert(
                self._our_faction_id, name=us.name if us else None, is_ours=True
            )
            enemy_row = await factions.upsert(
                opponent.faction_id if opponent else 0,
                name=opponent.name if opponent else None,
                is_ours=False,
            )

            if existing is None:
                status = assessment.state.value
                new_war = War(
                    guild_id=self._guild_id,
                    torn_war_id=war.war_id,
                    our_faction_id=our_row.id,
                    our_faction_name=our_row.name,
                    enemy_faction_id=enemy_row.id,
                    enemy_faction_name=enemy_row.name,
                    status=status,
                    result=_result_for(war.winner_faction_id, self._our_faction_id),
                    war_start=war.start,
                    war_end=war.end,
                )
                wars.add(new_war)
                await session.flush()  # populate new_war.id
                WarLogRepository(session).add(
                    war_registered_event(new_war.id, enemy_faction_name=enemy_row.name)
                )
                logger.info(
                    "war.registered",
                    torn_war_id=war.war_id,
                    state=status,
                    enemy_faction_id=opponent.faction_id if opponent else None,
                    enemy_faction_name=opponent.name if opponent else None,
                )
                return _Reconciled(
                    war_row_id=new_war.id,
                    enemy_torn_faction_id=opponent.faction_id if opponent else None,
                    enemy_faction_name=opponent.name if opponent else None,
                    is_new=True,
                    is_active=assessment.state is not WarState.WAR_FINISHED,
                    log_event_added=True,
                )

            # Known war — apply any state / result / timing changes.
            was_status = existing.status
            status_changed = was_status != assessment.state.value
            new_result = _result_for(war.winner_faction_id, self._our_faction_id)
            result_changed = bool(new_result and existing.result != new_result)

            changed = False
            if status_changed:
                existing.status = assessment.state.value
                changed = True
            if war.end and existing.war_end is None:
                existing.war_end = war.end
                changed = True
            if existing.war_start is None and war.start is not None:
                existing.war_start = war.start
                changed = True
            if result_changed:
                existing.result = new_result
                changed = True
            if assessment.state is WarState.WAR_FINISHED and existing.war_end is None:
                existing.war_end = utcnow()
                changed = True

            if not changed:
                logger.debug("war.unchanged", torn_war_id=war.war_id, state=existing.status)
                return None

            log_added = False
            if status_changed and assessment.state is WarState.WAR_FINISHED:
                logger.info(
                    "war.state_changed",
                    torn_war_id=war.war_id,
                    was=was_status,
                    now=assessment.state.value,
                )
                WarLogRepository(session).add(
                    war_finished_event(existing.id, result=existing.result)
                )
                # Only for a war we watched end: one first seen already finished
                # has no hit history, so its scoreboard would be empty.
                WarLogRepository(session).add(war_summary_event(existing.id))
                log_added = True
            elif status_changed:
                logger.info(
                    "war.state_changed",
                    torn_war_id=war.war_id,
                    was=was_status,
                    now=assessment.state.value,
                )
                WarLogRepository(session).add(
                    state_change_event(existing.id, was=was_status, now=assessment.state.value)
                )
                log_added = True
            elif result_changed:
                WarLogRepository(session).add(
                    war_finished_event(existing.id, result=existing.result)
                )
                log_added = True

            return _Reconciled(
                war_row_id=existing.id,
                enemy_torn_faction_id=None,
                enemy_faction_name=None,
                is_new=False,
                is_active=assessment.state is not WarState.WAR_FINISHED,
                log_event_added=log_added,
            )
