"""The ``/spy`` command group — confirmed spy reports (spec §24, Phase 8).

``/spy add`` opens a box to paste a spy report into (a modal keeps the line breaks a
slash-command option would flatten, and holds far more text). Reports are saved per
server in ``confirmed_stats`` — one faction's intel never shows in another's server —
and a recent one then outranks the FFScouter estimate on the target board, /targets
and /matchups (app.war.spy.best_strength).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from sqlalchemy import delete

from app.database.models import ConfirmedStat, Player
from app.database.repositories import ConfirmedStatRepository, PlayerRepository
from app.discord.embeds import base_embed, spy_report_text
from app.discord.permissions import requires_intel_access
from app.utils.logging import get_logger
from app.utils.time import utcnow
from app.war.lookup import resolve_player
from app.war.spy import parse_spy_report

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.discord.bot import TornWarIntelBot

logger = get_logger(__name__)

_NOTE_MAX = 200  # confirmed_stats.note is String(256)


async def _find_or_add_player(session: AsyncSession, query: str) -> Player | None:
    """A stored player by name or ID; a bare Torn ID the bot hasn't met yet is added,
    so a spy on someone outside any war roster so far can still be saved."""
    player = await resolve_player(session, query)
    cleaned = query.strip().lstrip("[").rstrip("]").strip()
    if player is None and cleaned.isdigit():
        player = await PlayerRepository(session).upsert(int(cleaned))
        await session.flush()
    return player


def _label(player: Player) -> str:
    return f"{player.name or 'Player'} [{player.torn_player_id}]"


class _SpyModal(discord.ui.Modal):
    def __init__(self, bot: TornWarIntelBot, player_query: str) -> None:
        super().__init__(title="Paste a spy report")
        self._bot = bot
        self._query = player_query
        self.report: discord.ui.TextInput[_SpyModal] = discord.ui.TextInput(
            label="Spy report",
            style=discord.TextStyle.paragraph,
            placeholder="Strength: 1,234,567\nDefense: ...\nSpeed: ...\nDexterity: ...",
            max_length=4000,
        )
        self.note: discord.ui.TextInput[_SpyModal] = discord.ui.TextInput(
            label="Note (optional)", required=False, max_length=_NOTE_MAX
        )
        self.add_item(self.report)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        services = self._bot.services
        if services is None or interaction.guild_id is None:
            await interaction.response.send_message(
                embed=base_embed("Not ready", description="The bot is still starting up."),
                ephemeral=True,
            )
            return
        parsed = parse_spy_report(self.report.value)
        if parsed.is_empty:
            await interaction.response.send_message(
                embed=base_embed(
                    "No stats found",
                    description="I couldn't find Strength / Defense / Speed / Dexterity / "
                    "Total numbers in that text. Nothing was saved.",
                ),
                ephemeral=True,
            )
            return

        async with services.database.session() as session:
            player = await _find_or_add_player(session, self._query)
            if player is None:
                await interaction.response.send_message(
                    embed=base_embed(
                        "Player not found",
                        description=f"No stored player called `{self._query}`. "
                        "Use their Torn ID instead. Nothing was saved.",
                    ),
                    ephemeral=True,
                )
                return
            if parsed.player_id is not None and parsed.player_id != player.torn_player_id:
                await interaction.response.send_message(
                    embed=base_embed(
                        "Wrong player?",
                        description=f"That report mentions [{parsed.player_id}], but you "
                        f"chose {_label(player)}. Nothing was saved.",
                    ),
                    ephemeral=True,
                )
                return
            row = ConfirmedStatRepository(session).add(
                ConfirmedStat(
                    player_id=player.id,
                    guild_id=interaction.guild_id,
                    strength=parsed.strength,
                    defense=parsed.defense,
                    speed=parsed.speed,
                    dexterity=parsed.dexterity,
                    total=parsed.best_total,
                    reported_by_discord_id=interaction.user.id,
                    reported_at=utcnow(),
                    note=self.note.value.strip() or None,
                )
            )
            label = _label(player)
            text = spy_report_text(row)

        logger.info(
            "spy.report_saved",
            guild_id=interaction.guild_id,
            torn_player_id=player.torn_player_id,
            has_total=parsed.best_total is not None,
        )
        await interaction.response.send_message(
            embed=base_embed(
                f"🔍 Spy report saved — {label}",
                description=f"{text}\n\nThe target board picks it up on its next refresh.",
            ),
            ephemeral=True,
        )


def register_spy(bot: TornWarIntelBot) -> None:
    spy = app_commands.Group(
        name="spy", description="Confirmed spy reports for enemies.", guild_only=True
    )

    def not_ready() -> discord.Embed:
        return base_embed("Not ready", description="The bot is still starting up.")

    @spy.command(name="add", description="Save a spy report — paste it into the box that opens.")
    @app_commands.describe(player="Torn player name or ID")
    @requires_intel_access()
    async def spy_add(interaction: discord.Interaction, player: str) -> None:
        await interaction.response.send_modal(_SpyModal(bot, player))

    @spy.command(name="show", description="Show this server's latest spy report on a player.")
    @app_commands.describe(player="Torn player name or ID")
    @requires_intel_access()
    async def spy_show(interaction: discord.Interaction, player: str) -> None:
        services = bot.services
        if services is None or interaction.guild_id is None:
            await interaction.response.send_message(embed=not_ready(), ephemeral=True)
            return
        async with services.database.session() as session:
            found = await resolve_player(session, player)
            row = (
                await ConfirmedStatRepository(session).latest_for_player(
                    interaction.guild_id, found.id
                )
                if found is not None
                else None
            )
        if found is None or row is None:
            embed = base_embed("No spy report", description=f"Nothing saved for `{player}`.")
        else:
            embed = base_embed(f"🔍 {_label(found)}", description=spy_report_text(row))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @spy.command(name="remove", description="Delete this server's latest spy report on a player.")
    @app_commands.describe(player="Torn player name or ID")
    @requires_intel_access()
    async def spy_remove(interaction: discord.Interaction, player: str) -> None:
        services = bot.services
        if services is None or interaction.guild_id is None:
            await interaction.response.send_message(embed=not_ready(), ephemeral=True)
            return
        async with services.database.session() as session:
            found = await resolve_player(session, player)
            row = (
                await ConfirmedStatRepository(session).latest_for_player(
                    interaction.guild_id, found.id
                )
                if found is not None
                else None
            )
            if found is not None and row is not None:
                await session.execute(delete(ConfirmedStat).where(ConfirmedStat.id == row.id))
        if found is None or row is None:
            embed = base_embed("No spy report", description=f"Nothing saved for `{player}`.")
        else:
            logger.info(
                "spy.report_removed",
                guild_id=interaction.guild_id,
                torn_player_id=found.torn_player_id,
            )
            embed = base_embed(
                "🗑️ Spy report removed",
                description=f"Deleted the latest report on {_label(found)}. "
                "Any older report for them is used again.",
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.tree.add_command(spy)
