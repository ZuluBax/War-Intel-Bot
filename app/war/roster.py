"""Enemy roster assembly (spec §10, §27).

Flow when a war is registered (and on every later refresh):

    1. FactionService.get_members(enemy_id)  -> upsert Player + WarPlayer rows,
       snapshot each member's status into player_status_history
    2. FFScouterClient.get_stats([...ids])   -> append a StatEstimate row per
       player that has an estimate (append-only history — spec §12)
    3. return an assembled Roster for the Discord layer to render

If FFScouter is unavailable the roster is still built from Torn data plus each
player's last known estimate from the database — a failed estimate refresh never
deletes the previous one (spec §27).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import Database
from app.database.models import Faction, FlightRecord, Overdose, StatEstimate, War
from app.database.models import PlayerStatusHistory as PlayerStatusHistoryModel
from app.database.repositories import (
    ActivityRepository,
    FlightRecordRepository,
    OverdoseRepository,
    PlayerRepository,
    StatEstimateRepository,
    StatusHistoryRepository,
    WarLogRepository,
    WarPlayerRepository,
)
from app.ffscouter.client import FFScouterClient
from app.ffscouter.errors import FFScouterError
from app.ffscouter.models import FFScouterStat
from app.torn.faction import FactionService
from app.torn.models import FactionMember, PlayerState
from app.utils.logging import get_logger
from app.utils.time import utcnow
from app.war.activity import activity_slot
from app.war.live_status import classify_flight, completed_flight
from app.war.overdose import overdose_drug
from app.war.warlog import overdose_event

logger = get_logger(__name__)

# A status change only counts as "seen happening" if we looked this recently before it.
# Three status-refresh cycles: one missed poll must not throw away a flight's take-off time.
DEFAULT_OBSERVATION_GAP = timedelta(seconds=180)
# Fallback length of an overdose stay when Torn gave no release time (seen: 80h-83h20m).
_LONGEST_OVERDOSE_STAY = timedelta(hours=84)


@dataclass
class RosterEntry:
    player_id: int
    name: str | None = None
    level: int | None = None
    faction_id: int | None = None
    state: PlayerState = PlayerState.UNKNOWN
    # The rest of Torn's status, for the live target board (hospital release, flights).
    status_description: str | None = None
    status_until: datetime | None = None
    plane_type: str | None = None
    status_since: datetime | None = None  # None = we never saw this status begin
    online_status: str | None = None  # Torn's "Online" / "Idle" / "Offline"
    estimated_bs: float | None = None
    estimated_bs_human: str | None = None
    fair_fight: float | None = None
    estimate_timestamp: datetime | None = None
    estimate_is_fresh: bool = False  # True if refreshed this cycle, False if from DB


@dataclass
class Roster:
    war_id: int
    enemy_faction_id: int
    enemy_faction_name: str | None = None
    entries: list[RosterEntry] = field(default_factory=list)
    estimates_available: bool = True  # False if FFScouter could not be reached

    def sorted_by_strength(self, *, descending: bool = True) -> list[RosterEntry]:
        """Entries ordered by estimated BS; unknown estimates always sort last."""

        def key(entry: RosterEntry) -> tuple[bool, float]:
            bs = float(entry.estimated_bs or 0)
            return (entry.estimated_bs is None, -bs if descending else bs)

        return sorted(self.entries, key=key)


@dataclass
class RosterDiff:
    """What changed in the enemy roster between two refreshes (spec §32)."""

    roster: Roster
    joined: list[tuple[int, str | None]]  # (torn player id, name) newly on the roster
    left: list[tuple[int, str | None]]  # (torn player id, name) no longer in the faction


def _status_key(war_id: int, m: FactionMember) -> tuple[object, ...]:
    """What makes a status snapshot worth storing again. A hospital/jail description is
    a countdown ("In hospital for 3 hrs 12 mins") that changes every minute on its own,
    so while there is a release time the time itself stands in for the text."""
    s = m.status
    text = s.description if s.until is None else None
    return (war_id, s.state, text, s.until, s.plane_type, m.online_status)


class EnemyRosterService:
    def __init__(
        self,
        database: Database,
        faction_service: FactionService,
        ffscouter: FFScouterClient | None,
        *,
        observation_gap: timedelta = DEFAULT_OBSERVATION_GAP,
    ) -> None:
        self._db = database
        self._faction = faction_service
        self._ffscouter = ffscouter
        self._observation_gap = observation_gap
        # Last activity slot stored per Torn player id, so an unchanged last_action (most
        # players, most minutes) costs no database round trip.
        self._activity_stored: dict[int, datetime] = {}
        # Last status snapshot stored per Torn player id. A row is only written when the
        # status changes: at a 15 s refresh, a row per enemy per refresh would fill the
        # free Supabase plan's 500 MB (read-only past that) in about two wars. Every reader
        # only wants a player's latest row, so skipping unchanged ones loses nothing.
        self._status_stored: dict[int, tuple[object, ...]] = {}
        # Torn player id -> release time last stored for their current overdose, so an
        # ongoing ~3-day stay costs no database round trip per refresh.
        self._overdose_until: dict[int, datetime | None] = {}

    async def build(
        self,
        war_id: int,
        enemy_faction_id: int,
        *,
        enemy_faction_name: str | None = None,
        fetch_estimates: bool = True,
    ) -> Roster:
        members = (await self._faction.get_members(enemy_faction_id)).members
        logger.info(
            "roster.members_fetched",
            war_id=war_id,
            enemy_faction_id=enemy_faction_id,
            count=len(members),
        )

        # 1. Persist players + war links + a status snapshot.
        internal_ids: dict[int, int] = {}  # torn player id -> Player.id
        status_since: dict[int, datetime | None] = {}  # torn player id -> when status began
        now = utcnow()
        written: dict[int, tuple[object, ...]] = {}  # remembered only once committed
        async with self._db.session() as session:
            players = PlayerRepository(session)
            war_players = WarPlayerRepository(session)
            status_history = StatusHistoryRepository(session)
            activity = ActivityRepository(session)
            await activity.record_watch_slot(war_id, activity_slot(now))
            for m in members:
                player = await players.upsert(
                    m.player_id, name=m.name, level=m.level, faction_id=enemy_faction_id
                )
                internal_ids[m.player_id] = player.id
                link = await war_players.upsert(war_id, player.id, side="ENEMY")
                flight = completed_flight(
                    link.status_description,
                    link.status_since,
                    link.status_checked_at,
                    m.status.description,
                    now=now,
                    max_gap=self._observation_gap,
                )
                if flight is not None:
                    await self._record_flight(session, player.id, *flight)
                if m.last_action is not None:
                    slot = activity_slot(m.last_action)
                    if self._activity_stored.get(m.player_id) != slot:
                        await activity.record_player_slot(player.id, slot)
                        self._activity_stored[m.player_id] = slot
                # Before this refresh's snapshot is added: it tells whether we had
                # looked at this player in this war before.
                await self._observe_overdose(session, war_id, player.id, m, now=now)
                status_since[m.player_id] = war_players.observe_status(
                    link, m.status.description, now=now, max_gap=self._observation_gap
                )
                snapshot = _status_key(war_id, m)
                if self._status_stored.get(m.player_id) != snapshot:
                    status_history.add(
                        PlayerStatusHistoryModel(
                            player_id=player.id,
                            war_id=war_id,
                            state=m.status.state.value,
                            description=m.status.description,
                            until=m.status.until,
                            plane_type=m.status.plane_type,
                            online_status=m.online_status,
                        )
                    )
                    written[m.player_id] = snapshot

        self._status_stored.update(written)

        # 2. Fetch estimates (best effort). No FFScouter key for this server, or a
        #    status-only refresh that deliberately skips it (Phase 5 status tick) ->
        #    skip cleanly; the roster still shows any previously stored estimate.
        stats: dict[int, FFScouterStat] = {}
        estimates_available = True
        if fetch_estimates:
            estimates_available = self._ffscouter is not None
            if self._ffscouter is not None:
                try:
                    stats = await self._ffscouter.get_stats(list(internal_ids))
                except FFScouterError as exc:
                    estimates_available = False
                    logger.warning("roster.ffscouter_unavailable", war_id=war_id, detail=str(exc))

        # 3. Append a StatEstimate row per fresh estimate.
        async with self._db.session() as session:
            estimate_repo = StatEstimateRepository(session)
            for torn_id, internal_id in internal_ids.items():
                stat = stats.get(torn_id)
                if stat is None or not stat.has_estimate:
                    continue
                estimate_repo.add(
                    StatEstimate(
                        player_id=internal_id,
                        estimated_bs=stat.bs_estimate,
                        estimated_bs_human=stat.bs_estimate_human,
                        bss_public=stat.bss_public,
                        fair_fight=stat.fair_fight,
                        source=stat.source or "ffscouter",
                        estimate_timestamp=stat.last_updated,
                    )
                )

        # 4. Assemble the roster, falling back to the latest DB estimate.
        entries = await self._assemble_entries(
            members, internal_ids, stats, enemy_faction_id, status_since
        )
        return Roster(
            war_id=war_id,
            enemy_faction_id=enemy_faction_id,
            enemy_faction_name=enemy_faction_name,
            entries=entries,
            estimates_available=estimates_available,
        )

    async def _observe_overdose(
        self,
        session: AsyncSession,
        war_id: int,
        player_id: int,
        m: FactionMember,
        *,
        now: datetime,
    ) -> None:
        """Record an overdose hospital stay once, and keep its release time current."""
        drug = overdose_drug(m.status.details) if m.status.state is PlayerState.HOSPITAL else None
        if drug is None:
            self._overdose_until.pop(m.player_id, None)
            return
        until = m.status.until
        if m.player_id in self._overdose_until and self._overdose_until[m.player_id] == until:
            return

        overdoses = OverdoseRepository(session)
        latest = await overdoses.latest(war_id, player_id)
        # Same stay if the stored one hasn't ended yet. Its release time is kept current
        # below, so a stay cut short by meds ends it and a later overdose is a new row.
        stored_end = (
            (latest.hospital_until or latest.first_seen_at + _LONGEST_OVERDOSE_STAY)
            if latest is not None
            else None
        )
        if latest is not None and stored_end is not None and stored_end > now:
            if latest.hospital_until != until:
                latest.hospital_until = until
            self._overdose_until[m.player_id] = until
            return

        before_watch = not await StatusHistoryRepository(session).seen_in_war(player_id, war_id)
        overdoses.add(
            Overdose(
                war_id=war_id,
                player_id=player_id,
                drug=drug,
                first_seen_at=now,
                hospital_until=until,
                before_watch=before_watch,
            )
        )
        if not before_watch:
            WarLogRepository(session).add(
                overdose_event(
                    war_id, player_id=m.player_id, name=m.name, drug=drug, hospital_until=until
                )
            )
        self._overdose_until[m.player_id] = until
        logger.info(
            "roster.overdose_seen",
            war_id=war_id,
            player_id=m.player_id,
            drug=drug,
            before_watch=before_watch,
        )

    @staticmethod
    async def _record_flight(
        session: AsyncSession, player_id: int, place: str, departed: datetime, landed: datetime
    ) -> None:
        # The plane is only on the status while flying: take it from the last snapshot.
        last = await StatusHistoryRepository(session).latest_for_player(player_id)
        plane_type = last.plane_type if last is not None else None
        method = classify_flight(place, landed - departed, plane_type)
        FlightRecordRepository(session).add(
            FlightRecord(
                player_id=player_id,
                place=place,
                departed_at=departed,
                landed_at=landed,
                plane_type=plane_type,
                method=method,
            )
        )
        logger.info(
            "roster.flight_recorded",
            player_id=player_id,
            place=place,
            minutes=round((landed - departed).total_seconds() / 60, 1),
            method=method,
        )

    async def refresh(self, war_id: int, *, fetch_estimates: bool = True) -> Roster:
        """Rebuild the roster for an existing war (used by the Phase 5 scheduler)."""
        async with self._db.session() as session:
            result = await session.execute(
                select(Faction.torn_faction_id, Faction.name)
                .join(War, War.enemy_faction_id == Faction.id)
                .where(War.id == war_id)
            )
            row = result.first()
        if row is None:
            raise ValueError(f"war {war_id} not found or has no enemy faction")
        enemy_torn_id, enemy_name = row
        return await self.build(
            war_id, enemy_torn_id, enemy_faction_name=enemy_name, fetch_estimates=fetch_estimates
        )

    async def refresh_with_diff(self, war_id: int, *, fetch_estimates: bool = True) -> RosterDiff:
        """Rebuild the roster and report who joined/left the enemy faction since
        the last refresh (spec §32) — drives the war-log's roster events."""
        async with self._db.session() as session:
            before = await WarPlayerRepository(session).active_enemy_torn_ids(war_id)

        roster = await self.refresh(war_id, fetch_estimates=fetch_estimates)

        current = {e.player_id for e in roster.entries}
        name_by_id = {e.player_id: e.name for e in roster.entries}
        joined_ids = current - before
        left_ids = before - current

        joined = [(pid, name_by_id.get(pid)) for pid in joined_ids]
        left: list[tuple[int, str | None]] = []
        if left_ids:
            async with self._db.session() as session:
                players = PlayerRepository(session)
                for pid in left_ids:
                    player = await players.get_by_torn_id(pid)
                    left.append((pid, player.name if player else None))
                await WarPlayerRepository(session).mark_left(war_id, left_ids)

        return RosterDiff(roster=roster, joined=joined, left=left)

    async def _assemble_entries(
        self,
        members: tuple[FactionMember, ...],
        internal_ids: dict[int, int],
        stats: dict[int, FFScouterStat],
        enemy_faction_id: int,
        status_since: dict[int, datetime | None],
    ) -> list[RosterEntry]:
        # Prime last-known estimates from the DB for any player without a fresh one.
        need_fallback = [
            internal_ids[m.player_id]
            for m in members
            if not (stats.get(m.player_id) and stats[m.player_id].has_estimate)
        ]
        fallback: dict[int, StatEstimate] = {}
        if need_fallback:
            async with self._db.session() as session:
                repo = StatEstimateRepository(session)
                for internal_id in need_fallback:
                    latest = await repo.latest_for_player(internal_id)
                    if latest is not None:
                        fallback[internal_id] = latest

        entries: list[RosterEntry] = []
        for m in members:
            internal_id = internal_ids[m.player_id]
            fresh = stats.get(m.player_id)
            if fresh is not None and fresh.has_estimate:
                entries.append(
                    RosterEntry(
                        player_id=m.player_id,
                        name=m.name,
                        level=m.level,
                        faction_id=enemy_faction_id,
                        state=m.status.state,
                        status_description=m.status.description,
                        status_until=m.status.until,
                        plane_type=m.status.plane_type,
                        status_since=status_since.get(m.player_id),
                        online_status=m.online_status,
                        estimated_bs=fresh.bs_estimate,
                        estimated_bs_human=fresh.bs_estimate_human,
                        fair_fight=fresh.fair_fight,
                        estimate_timestamp=fresh.last_updated,
                        estimate_is_fresh=True,
                    )
                )
                continue
            prior = fallback.get(internal_id)
            entries.append(
                RosterEntry(
                    player_id=m.player_id,
                    name=m.name,
                    level=m.level,
                    faction_id=enemy_faction_id,
                    state=m.status.state,
                    status_description=m.status.description,
                    status_until=m.status.until,
                    plane_type=m.status.plane_type,
                    status_since=status_since.get(m.player_id),
                    online_status=m.online_status,
                    estimated_bs=prior.estimated_bs if prior else None,
                    estimated_bs_human=prior.estimated_bs_human if prior else None,
                    fair_fight=prior.fair_fight if prior else None,
                    estimate_timestamp=prior.estimate_timestamp if prior else None,
                    estimate_is_fresh=False,
                )
            )
        return entries
