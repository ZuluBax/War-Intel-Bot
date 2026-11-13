"""Bridges a war + roster to the Discord war workspace.

Implements :class:`app.war.manager.WarPublisher`. The war manager calls
``publish_war`` after it builds a roster; everything Discord-specific lives here
so the ``app.war`` package stays free of ``discord`` imports.
"""

from __future__ import annotations

from app.database.database import Database
from app.database.repositories import (
    DiscordChannelRepository,
    OverdoseRepository,
    WarRepository,
)
from app.discord.abroad_picker import abroad_picker
from app.discord.activity_board import activity_embed
from app.discord.embeds import roster_embed, war_info_embed, war_score_embed
from app.discord.gateway import GatewayNotReady
from app.discord.target_board import (
    abroad_countries,
    target_board_cleared_embed,
    target_board_embed,
)
from app.discord.war_channels import SCORE_PURPOSE, WarBoard, WarChannelManager, WarLogBoard
from app.utils.logging import get_logger
from app.war.activity import ActivitySummary
from app.war.roster import Roster
from app.war.score import WarScore
from app.war.target_service import TargetView

logger = get_logger(__name__)


class DiscordWarPublisher:
    def __init__(
        self,
        database: Database,
        channel_manager: WarChannelManager,
        board: WarBoard,
        log_board: WarLogBoard,
    ) -> None:
        self._db = database
        self._channels = channel_manager
        self._board = board
        self._log_board = log_board

    async def publish_war(self, war_row_id: int, roster: Roster) -> None:
        async with self._db.session() as session:
            war = await WarRepository(session).get_by_id(war_row_id)
        if war is None:
            return

        try:
            await self._channels.ensure_workspace(war_row_id)
            await self._board.publish(
                war_row_id,
                "information",
                war_info_embed(
                    our_faction_name=war.our_faction_name,
                    enemy_faction_name=war.enemy_faction_name,
                    enemy_member_count=len(roster.entries),
                    status=war.status,
                    torn_war_id=war.torn_war_id,
                    war_start=war.war_start,
                    war_end=war.war_end,
                ),
            )
            await self._board.publish(war_row_id, "stats", roster_embed(roster))
        except GatewayNotReady:
            logger.info("war.publish_deferred_not_ready", war_row_id=war_row_id)
            return

        logger.info("war.published", war_row_id=war_row_id, players=len(roster.entries))

    async def publish_score(self, war_row_id: int, score: WarScore) -> bool:
        """Edit the small score box in ``#war-information``; True once it is on Discord."""
        async with self._db.session() as session:
            channels = DiscordChannelRepository(session)
            info = await channels.get(war_row_id, "information")
            if info is None:
                return False  # workspace not built yet — the next tick retries
            # Re-pointed every call so the box follows the information channel if that
            # was ever recreated; WarBoard resends if the old message is gone.
            await channels.upsert_channel(war_row_id, SCORE_PURPOSE, info.channel_id)

        return await self._board.publish(war_row_id, SCORE_PURPOSE, war_score_embed(score))

    async def publish_targets(self, war_row_id: int, targets: list[TargetView]) -> None:
        async with self._db.session() as session:
            overdoses = await OverdoseRepository(session).since_watch(war_row_id)
        try:
            # One embed per message: a roster too big for one message comes as two.
            await self._board.publish_messages(
                war_row_id,
                "targets",
                [[embed] for embed in target_board_embed(targets, overdoses=overdoses)],
                view=abroad_picker(abroad_countries(targets)),
            )
        except GatewayNotReady:
            logger.info("war.publish_targets_deferred_not_ready", war_row_id=war_row_id)
            return
        logger.info("war.targets_published", war_row_id=war_row_id, targets=len(targets))

    async def publish_activity(self, war_row_id: int, summary: ActivitySummary) -> None:
        try:
            await self._board.publish(war_row_id, "activity", activity_embed(summary))
        except GatewayNotReady:
            logger.info("war.publish_activity_deferred_not_ready", war_row_id=war_row_id)
            return
        logger.info(
            "war.activity_published",
            war_row_id=war_row_id,
            watched_hours=round(summary.watched_hours, 1),
            active_now=summary.active_now,
        )

    async def clear_targets(self, war_row_id: int) -> None:
        """Blank ``#target-board`` once the war ends — stale attack-now advice for a
        war that's over is worse than no advice, so it clears until the next war."""
        try:
            await self._board.publish(
                war_row_id, "targets", target_board_cleared_embed(), view=abroad_picker([])
            )
        except GatewayNotReady:
            logger.info("war.clear_targets_deferred_not_ready", war_row_id=war_row_id)
            return
        logger.info("war.targets_cleared", war_row_id=war_row_id)

    async def publish_log(self, war_row_id: int) -> None:
        # Hits go first so a "war has ended" line never lands above the last hits.
        try:
            await self._log_board.publish_pending_hits(war_row_id)
        except Exception as exc:  # a hit-posting failure must not block the other log events
            logger.error(
                "war.publish_hits_failed",
                war_row_id=war_row_id,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
        await self._log_board.publish_pending(war_row_id)
