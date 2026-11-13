"""Slash-command registration (spec §20).

Command surface (fixed since Phase 1; behaviour filled in per phase):

    /war            current war summary                     (intel access)  [Phase 4]
    /war-status     detailed current war status             (intel access)  [Phase 4]
    /enemies        enemy roster                             (intel access)  [Phase 4]
    /enemy <player> intelligence on one enemy                (intel access)  ✅ Phase 3
    /stats <player> latest estimated stats for one player    (intel access)  ✅ Phase 3
    /targets        target-priority list                     (intel access)  ✅ Phase 6
    /refresh        request a controlled refresh             (intel access)  ✅ Phase 5
    /history        previous wars                            (intel access)  ✅ Phase 7
    /spy add|show|remove  confirmed spy reports              (intel access)  ✅ Phase 8
    /matchups [player]    our player vs every enemy          (intel access)  ✅ Phase 8

Server configuration is ``/setup`` (admins only). The ``/config`` placeholder was dropped
in Phase 8 rather than built as a second place to change the same settings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from app.database.repositories import (
    ConfirmedStatRepository,
    StatEstimateRepository,
    StatusHistoryRepository,
)
from app.discord.embeds import (
    base_embed,
    history_embed,
    player_intel_embed,
    roster_embed,
    war_info_embed,
)
from app.discord.permissions import requires_intel_access
from app.discord.target_board import target_board_embed
from app.torn.models import PlayerState
from app.war.lookup import resolve_player
from app.war.multi_guild import RefreshOutcome
from app.war.summary import load_latest_war, load_roster_view, load_target_view

if TYPE_CHECKING:
    from app.discord.bot import TornWarIntelBot


def register_commands(bot: TornWarIntelBot) -> None:
    from app.discord.matchup_commands import register_matchups
    from app.discord.setup_commands import register_setup
    from app.discord.spy_commands import register_spy

    tree = bot.tree
    register_setup(bot)
    register_spy(bot)
    register_matchups(bot)

    async def _player_intel_embed(
        query: str, guild_id: int | None, *, compact: bool
    ) -> discord.Embed:
        services = bot.services
        if services is None:
            return base_embed("Not ready", description="The bot is still starting up.")

        async with services.database.session() as session:
            player = await resolve_player(session, query)
            if player is None:
                return base_embed(
                    "Player not found",
                    description=(
                        f"No stored data for `{query}`. The bot only knows players "
                        "from a current or past war roster. Try a Torn ID."
                    ),
                )

            status_row = await StatusHistoryRepository(session).latest_for_player(player.id)
            state = PlayerState(status_row.state) if status_row else PlayerState.UNKNOWN

            estimates = StatEstimateRepository(session)
            trend = await services.intelligence.summarise_player(estimates, player.id)
            latest = await estimates.latest_for_player(player.id)
            spy = (
                await ConfirmedStatRepository(session).latest_for_player(guild_id, player.id)
                if guild_id is not None
                else None
            )

        return player_intel_embed(
            name=player.name,
            player_id=player.torn_player_id,
            level=player.level,
            state=state,
            trend=trend,
            fair_fight=latest.fair_fight if latest else None,
            compact=compact,
            spy=spy,
        )

    async def _war_embed(guild_id: int | None, *, detailed: bool) -> discord.Embed:
        services = bot.services
        if services is None or guild_id is None:
            return base_embed("Not ready", description="The bot is still starting up.")
        async with services.database.session() as session:
            war = await load_latest_war(session, guild_id)
            if war is None:
                return base_embed(
                    "No war on record",
                    description="No ranked war has been detected for this server yet. "
                    "Have you run `/setup`?",
                )
            roster = await load_roster_view(session, war)
        embed = war_info_embed(
            our_faction_name=war.our_faction_name,
            enemy_faction_name=war.enemy_faction_name,
            enemy_member_count=len(roster.entries),
            status=war.status,
            torn_war_id=war.torn_war_id,
            war_start=war.war_start,
            war_end=war.war_end,
        )
        if detailed and war.result:
            embed.add_field(name="Result", value=war.result, inline=True)
        if detailed:
            ranked = roster.sorted_by_strength()[:3]
            if ranked and ranked[0].estimated_bs is not None:
                top = "\n".join(
                    f"{i + 1}. {e.name or e.player_id}" for i, e in enumerate(ranked)
                )
                embed.add_field(name="Strongest enemies (est.)", value=top, inline=False)
        return embed

    @tree.command(name="war", description="Show the current war summary.")
    @app_commands.guild_only()
    @requires_intel_access()
    async def war(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=await _war_embed(interaction.guild_id, detailed=False), ephemeral=True
        )

    @tree.command(name="war-status", description="Show detailed current war status.")
    @app_commands.guild_only()
    @requires_intel_access()
    async def war_status(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=await _war_embed(interaction.guild_id, detailed=True), ephemeral=True
        )

    @tree.command(name="enemies", description="Show the enemy roster.")
    @app_commands.guild_only()
    @requires_intel_access()
    async def enemies(interaction: discord.Interaction) -> None:
        services = bot.services
        if services is None or interaction.guild_id is None:
            await interaction.response.send_message(
                embed=base_embed("Not ready", description="The bot is still starting up."),
                ephemeral=True,
            )
            return
        async with services.database.session() as session:
            war = await load_latest_war(session, interaction.guild_id)
            if war is None:
                await interaction.response.send_message(
                    embed=base_embed(
                        "No war on record",
                        description="No ranked war detected for this server yet. "
                        "Have you run `/setup`?",
                    ),
                    ephemeral=True,
                )
                return
            roster = await load_roster_view(session, war)
        await interaction.response.send_message(embeds=roster_embed(roster), ephemeral=True)

    @tree.command(name="enemy", description="Show intelligence for a specific enemy.")
    @app_commands.describe(player="Torn player name or ID")
    @requires_intel_access()
    async def enemy(interaction: discord.Interaction, player: str) -> None:
        embed = await _player_intel_embed(player, interaction.guild_id, compact=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="stats", description="Show the latest estimated stats for a player.")
    @app_commands.describe(player="Torn player name or ID")
    @requires_intel_access()
    async def stats(interaction: discord.Interaction, player: str) -> None:
        embed = await _player_intel_embed(player, interaction.guild_id, compact=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="targets", description="Show the target-priority list.")
    @app_commands.describe(available_only="Only show enemies you can attack right now")
    @app_commands.guild_only()
    @requires_intel_access()
    async def targets(interaction: discord.Interaction, available_only: bool = False) -> None:
        services = bot.services
        if services is None or interaction.guild_id is None:
            await interaction.response.send_message(
                embed=base_embed("Not ready", description="The bot is still starting up."),
                ephemeral=True,
            )
            return
        async with services.database.session() as session:
            war = await load_latest_war(session, interaction.guild_id)
            if war is None:
                await interaction.response.send_message(
                    embed=base_embed(
                        "No war on record",
                        description="No ranked war detected for this server yet. "
                        "Have you run `/setup`?",
                    ),
                    ephemeral=True,
                )
                return
            views = await load_target_view(session, war)
        if available_only:
            views = [v for v in views if v.is_available]
        await interaction.response.send_message(embeds=target_board_embed(views), ephemeral=True)

    _REFRESH_REPLY: dict[RefreshOutcome, tuple[str, str]] = {
        RefreshOutcome.REFRESHED: (
            "✅ Refreshed",
            "War data has been refreshed — check the war channels for the latest.",
        ),
        RefreshOutcome.RATE_LIMITED: (
            "⏳ Please wait",
            "A refresh was requested too recently. It happens automatically anyway "
            "— try again shortly.",
        ),
        RefreshOutcome.NOT_CONFIGURED: (
            "Not configured",
            "This server hasn't run `/setup` yet.",
        ),
        RefreshOutcome.NO_ACTIVE_WAR: (
            "No active war",
            "There is no active ranked war to refresh right now.",
        ),
    }

    @tree.command(name="refresh", description="Request a controlled refresh of war data.")
    @app_commands.guild_only()
    @requires_intel_access()
    async def refresh(interaction: discord.Interaction) -> None:
        services = bot.services
        if services is None or interaction.guild_id is None:
            await interaction.response.send_message(
                embed=base_embed("Not ready", description="The bot is still starting up."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        outcome = await services.runner.manual_refresh(interaction.guild_id)
        title, description = _REFRESH_REPLY[outcome]
        await interaction.followup.send(
            embed=base_embed(title, description=description), ephemeral=True
        )

    @tree.command(name="history", description="Show previous wars.")
    @app_commands.guild_only()
    @requires_intel_access()
    async def history(interaction: discord.Interaction) -> None:
        services = bot.services
        if services is None or interaction.guild_id is None:
            await interaction.response.send_message(
                embed=base_embed("Not ready", description="The bot is still starting up."),
                ephemeral=True,
            )
            return
        wars = await services.war_history.recent(interaction.guild_id)
        await interaction.response.send_message(embed=history_embed(wars), ephemeral=True)
