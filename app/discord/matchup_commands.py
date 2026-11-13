"""``/matchups`` — one of our players against every enemy in the current war (Phase 8).

Who "you" are comes from the ``player`` option, else the ``[1234567]`` Torn ID that
Torn-verified servers put in nicknames. Your strength is this server's recent spy
report on you if there is one, else one FFScouter call (cached by the runner).
Enemy strengths come from the saved target ranking, so no Torn calls are made.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from app.database.models import Player
from app.database.repositories import ConfirmedStatRepository, PlayerRepository
from app.discord.embeds import base_embed
from app.discord.permissions import requires_intel_access
from app.discord.target_board import SPY_MARK, attack_url
from app.torn.models import PlayerState
from app.utils.formatting import format_stat, truncate
from app.war.lookup import resolve_player
from app.war.matchups import AVOID_ABOVE, GOOD_BELOW, Matchup, Matchups, build_matchups
from app.war.spy import Strength, best_strength
from app.war.summary import load_latest_war, load_target_view

if TYPE_CHECKING:
    from app.discord.bot import TornWarIntelBot

_NICK_ID = re.compile(r"\[(\d{1,10})\]")
_STATE_ICON = {
    PlayerState.OKAY: "🟢",
    PlayerState.HOSPITAL: "🔴",
    PlayerState.JAIL: "🟠",
    PlayerState.TRAVELING: "🔵",
    PlayerState.UNKNOWN: "⚪",
}
# Rows shown per group; the whole reply stays well inside one embed's 4096 characters.
_SHOW = {"good": 15, "even": 10, "avoid": 5}
_DESC_LIMIT = 3900
FOOTER = (
    "A rough guide from battle-strength totals — stat split, weapons and armour matter "
    f"too. {SPY_MARK} = from a spy report."
)


def torn_id_from_nickname(name: str | None) -> int | None:
    """The Torn ID in a "Name [1234567]" style server nickname."""
    match = _NICK_ID.search(name or "")
    return int(match.group(1)) if match else None


def _pct(ratio: float) -> str:
    return f"{round(ratio * 100)}%"


def _row(m: Matchup) -> str:
    t = m.target
    name = discord.utils.escape_markdown(truncate(t.name or str(t.player_id), 20))
    if t.player_id > 0:
        name = f"[{name}]({attack_url(t.player_id)})"
    stat = format_stat(t.estimated_bs) + (SPY_MARK if t.confirmed else "")
    level = t.level if t.level is not None else "?"
    return f"{_STATE_ICON.get(t.state, '⚪')} {name} L{level} · {stat} · {_pct(m.ratio)}"


def matchup_embed(
    *, our_name: str, our_strength: float, our_confirmed: bool, matchups: Matchups
) -> discord.Embed:
    groups = (
        ("good", f"✅ **GOOD FIGHTS** — under {_pct(GOOD_BELOW)} of you", matchups.good),
        ("even", f"⚖️ **EVEN** — {_pct(GOOD_BELOW)} to {_pct(AVOID_ABOVE)}", matchups.even),
        ("avoid", f"⛔ **AVOID** — over {_pct(AVOID_ABOVE)} of you", matchups.avoid),
    )
    lines: list[str] = []
    for key, header, items in groups:
        if not items:
            continue
        lines.append(f"{header} ({len(items)})")
        lines += [_row(m) for m in items[: _SHOW[key]]]
        if len(items) > _SHOW[key]:
            lines.append(f"…and {len(items) - _SHOW[key]} more")
        lines.append("")
    if matchups.unknown:
        lines.append(f"❔ {matchups.unknown} enemies have no strength number yet.")
    description = "\n".join(lines).strip() or "No enemies to compare against."
    if len(description) > _DESC_LIMIT:
        description = description[: _DESC_LIMIT - 1].rsplit("\n", 1)[0] + "\n…"
    you = format_stat(our_strength) + (SPY_MARK if our_confirmed else " (estimate)")
    embed = base_embed(f"🥊 Matchups — {truncate(our_name, 40)} · {you}")
    embed.description = description
    embed.set_footer(text=FOOTER)
    return embed


def register_matchups(bot: TornWarIntelBot) -> None:
    def reply(title: str, description: str) -> discord.Embed:
        return base_embed(title, description=description)

    @bot.tree.command(name="matchups", description="Which enemies you can beat, by strength.")
    @app_commands.describe(player="One of our players (name or Torn ID). Default: you.")
    @app_commands.guild_only()
    @requires_intel_access()
    async def matchups(interaction: discord.Interaction, player: str | None = None) -> None:
        services = bot.services
        guild_id = interaction.guild_id
        if services is None or guild_id is None:
            await interaction.response.send_message(
                embed=reply("Not ready", "The bot is still starting up."), ephemeral=True
            )
            return

        query = player
        if query is None:
            display = getattr(interaction.user, "display_name", None)
            nick_id = torn_id_from_nickname(display)
            if nick_id is None:
                await interaction.response.send_message(
                    embed=reply(
                        "Who are you in Torn?",
                        "Your server nickname has no `[Torn ID]` in it. "
                        "Run `/matchups player:<your Torn ID>`.",
                    ),
                    ephemeral=True,
                )
                return
            query = str(nick_id)

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with services.database.session() as session:
            us: Player | None = await resolve_player(session, query)
            cleaned = query.strip().lstrip("[").rstrip("]").strip()
            torn_id = us.torn_player_id if us else (int(cleaned) if cleaned.isdigit() else None)
            if us is None and torn_id is not None:
                us = await PlayerRepository(session).get_by_torn_id(torn_id)
            spy = (
                await ConfirmedStatRepository(session).latest_for_player(guild_id, us.id)
                if us is not None
                else None
            )
            war = await load_latest_war(session, guild_id)
            targets = await load_target_view(session, war) if war is not None else []

        if torn_id is None:
            await interaction.followup.send(
                embed=reply("Player not found", f"No stored player `{query}`. Use a Torn ID."),
                ephemeral=True,
            )
            return
        if war is None or not targets:
            await interaction.followup.send(
                embed=reply("No enemies yet", "There is no ranked enemy list for this server."),
                ephemeral=True,
            )
            return

        spy_total = spy.total if spy else None
        strength = best_strength(
            estimate=None,
            spy_total=spy_total,
            spy_reported_at=spy.reported_at if spy else None,
        )
        if not strength.confirmed:  # no trusted spy report: ask FFScouter
            estimate = await services.runner.strength_estimate(guild_id, torn_id)
            strength = Strength(estimate, confirmed=False)
            if estimate is None and spy_total is not None:
                strength = Strength(spy_total, confirmed=True)  # old report beats nothing
        if strength.value is None or strength.value <= 0:
            await interaction.followup.send(
                embed=reply(
                    "No strength number",
                    f"FFScouter has no estimate for [{torn_id}] and there is no spy report. "
                    "Add one with `/spy add`.",
                ),
                ephemeral=True,
            )
            return

        our_name = (us.name if us and us.name else None) or f"[{torn_id}]"
        embed = matchup_embed(
            our_name=our_name,
            our_strength=strength.value,
            our_confirmed=strength.confirmed,
            matchups=build_matchups(strength.value, targets),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
