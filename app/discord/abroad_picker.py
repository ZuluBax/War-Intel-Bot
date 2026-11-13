"""The "show one country" drop-down under ``#target-board``.

Discord messages cannot fold a section open or shut, and the board is one message that
everyone shares, so hiding countries on the board itself would hide them for the whole
server. Instead, picking a country answers with a private (ephemeral) copy of just that
country's enemies — only the person who picked sees it; the board does not change.

The drop-down is re-sent with every board refresh (so its choices follow who is abroad
now), and picks are handled in ``TornWarIntelBot.on_interaction`` by its fixed
``custom_id`` rather than by a registered view, so it keeps working after a restart.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import discord

from app.discord.embeds import base_embed
from app.discord.target_board import country_embed, country_flag
from app.utils.logging import get_logger
from app.war.summary import load_latest_war, load_target_view

if TYPE_CHECKING:
    from app.discord.bot import TornWarIntelBot

logger = get_logger(__name__)

ABROAD_PICKER_ID = "twi:abroad-country"
_MAX_OPTIONS = 25  # Discord's limit per drop-down; Torn has 11 destinations


def abroad_picker(countries: list[tuple[str, int]]) -> discord.ui.View:
    """The drop-down for these ``(country, enemies there)`` choices. With none, an
    empty view — sending that removes an old drop-down from the board."""
    view = discord.ui.View(timeout=None)
    if countries:
        view.add_item(
            discord.ui.Select(
                custom_id=ABROAD_PICKER_ID,
                placeholder="🔎 Show one country (only you will see it)",
                options=[
                    discord.SelectOption(
                        label=f"{country} ({count})", value=country, emoji=country_flag(country)
                    )
                    for country, count in countries[:_MAX_OPTIONS]
                ],
            )
        )
    return view


def _picked_country(interaction: discord.Interaction) -> str | None:
    if interaction.type is not discord.InteractionType.component:
        return None
    data: Mapping[str, Any] = cast(Mapping[str, Any], interaction.data or {})
    if data.get("custom_id") != ABROAD_PICKER_ID:
        return None
    values = data.get("values") or []
    return str(values[0]) if values else None


async def handle_abroad_pick(bot: TornWarIntelBot, interaction: discord.Interaction) -> bool:
    """Answer a pick from the drop-down. False if the interaction is not one.

    No intel-role check, unlike ``/targets``: the reply only repeats what the board the
    person just clicked on already shows them."""
    country = _picked_country(interaction)
    if country is None:
        return False

    # Reading the roster can take longer than Discord's 3-second answer limit.
    await interaction.response.defer(ephemeral=True, thinking=True)
    services = bot.services
    if services is None or interaction.guild_id is None:
        await interaction.followup.send(
            embed=base_embed("Not ready", description="The bot is still starting up."),
            ephemeral=True,
        )
        return True
    async with services.database.session() as session:
        war = await load_latest_war(session, interaction.guild_id)
        views = await load_target_view(session, war) if war is not None else []
    await interaction.followup.send(embeds=country_embed(views, country), ephemeral=True)
    logger.info("abroad_picker.shown", guild_id=interaction.guild_id, country=country)
    return True
