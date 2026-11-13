"""Read-side helpers: reconstruct a war summary / roster view from the database.

Used by the ``/war``, ``/war-status`` and ``/enemies`` commands so they render
the same data (and the same :func:`app.discord.embeds.roster_embed`) that the
live channels show, without re-hitting Torn or FFScouter.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Faction,
    Player,
    PlayerStatusHistory,
    StatEstimate,
    War,
    WarPlayer,
)
from app.database.repositories import (
    ConfirmedStatRepository,
    TargetRankingRepository,
    WarRepository,
)
from app.torn.models import PlayerState
from app.war.roster import Roster, RosterEntry
from app.war.spy import best_strength
from app.war.target_service import TargetView


async def load_active_war(session: AsyncSession, guild_id: int) -> War | None:
    return await WarRepository(session).get_active(guild_id)


async def load_latest_war(session: AsyncSession, guild_id: int) -> War | None:
    """This server's active war, or failing that its most recently created one."""
    active = await WarRepository(session).get_active(guild_id)
    if active is not None:
        return active
    latest: War | None = await session.scalar(
        select(War)
        .where(War.guild_id == guild_id)
        .order_by(War.created_at.desc())
        .limit(1)
    )
    return latest


async def load_roster_view(session: AsyncSession, war: War) -> Roster:
    enemy = await session.get(Faction, war.enemy_faction_id)
    enemy_torn_id = enemy.torn_faction_id if enemy else 0
    enemy_name = enemy.name if enemy else war.enemy_faction_name

    players = list(
        await session.scalars(
            select(Player)
            .join(WarPlayer, WarPlayer.player_id == Player.id)
            .where(WarPlayer.war_id == war.id, WarPlayer.side == "ENEMY")
        )
    )

    entries: list[RosterEntry] = []
    for player in players:
        estimate = await session.scalar(
            select(StatEstimate)
            .where(StatEstimate.player_id == player.id)
            .order_by(StatEstimate.observed_at.desc())
            .limit(1)
        )
        status_row = await session.scalar(
            select(PlayerStatusHistory)
            .where(PlayerStatusHistory.player_id == player.id)
            .order_by(PlayerStatusHistory.observed_at.desc())
            .limit(1)
        )
        state = PlayerState(status_row.state) if status_row else PlayerState.UNKNOWN
        entries.append(
            RosterEntry(
                player_id=player.torn_player_id,
                name=player.name,
                level=player.level,
                faction_id=enemy_torn_id,
                state=state,
                estimated_bs=estimate.estimated_bs if estimate else None,
                estimated_bs_human=estimate.estimated_bs_human if estimate else None,
                fair_fight=estimate.fair_fight if estimate else None,
                estimate_timestamp=estimate.estimate_timestamp if estimate else None,
                estimate_is_fresh=False,
            )
        )

    return Roster(
        war_id=war.id,
        enemy_faction_id=enemy_torn_id,
        enemy_faction_name=enemy_name,
        entries=entries,
    )


async def load_target_view(session: AsyncSession, war: War) -> list[TargetView]:
    """The persisted target-priority snapshot (Phase 6), re-read for ``/targets``
    without recomputing it — recomputation happens on the refresh cadence."""
    rows = await TargetRankingRepository(session).for_war(war.id)
    spies = await ConfirmedStatRepository(session).latest_for_players(
        war.guild_id, [row.player_id for row in rows]
    )
    views: list[TargetView] = []
    for row in rows:
        player = await session.get(Player, row.player_id)
        status_row = await session.scalar(
            select(PlayerStatusHistory)
            .where(PlayerStatusHistory.player_id == row.player_id)
            .order_by(PlayerStatusHistory.observed_at.desc())
            .limit(1)
        )
        estimate = await session.scalar(
            select(StatEstimate)
            .where(StatEstimate.player_id == row.player_id)
            .order_by(StatEstimate.observed_at.desc())
            .limit(1)
        )
        link = await session.scalar(
            select(WarPlayer).where(
                WarPlayer.war_id == war.id, WarPlayer.player_id == row.player_id
            )
        )
        spy = spies.get(row.player_id)
        strength = best_strength(
            estimate=estimate.estimated_bs if estimate else None,
            spy_total=spy.total if spy else None,
            spy_reported_at=spy.reported_at if spy else None,
        )
        state = PlayerState(status_row.state) if status_row else PlayerState.UNKNOWN
        # The take-off time belongs to the status the link last saw; only use it if the
        # latest recorded status is that same one.
        same_status = (
            link is not None
            and status_row is not None
            and link.status_description == status_row.description
        )
        views.append(
            TargetView(
                player_id=player.torn_player_id if player else 0,
                name=player.name if player else None,
                level=player.level if player else None,
                rank=row.rank,
                score=row.score,
                priority=row.priority,
                state=state,
                is_available=state is PlayerState.OKAY,
                estimated_bs=strength.value,
                confirmed=strength.confirmed,
                fair_fight=estimate.fair_fight if estimate else None,
                status_description=status_row.description if status_row else None,
                status_until=status_row.until if status_row else None,
                plane_type=status_row.plane_type if status_row else None,
                status_since=link.status_since if same_status and link else None,
                online_status=status_row.online_status if status_row else None,
            )
        )
    return views
