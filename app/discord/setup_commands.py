"""The ``/setup`` command group — per-server configuration (admins only).

Each Discord server configures which Torn faction it represents and supplies its
own Torn (and optionally FFScouter) API key. Keys are validated live against
Torn before they are saved, and every reply is ephemeral so a pasted key never
appears in the channel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from app.discord.embeds import base_embed
from app.discord.permissions import requires_admin
from app.discord.war_channels import CHANNEL_LAYOUT
from app.torn.client import TornClient
from app.torn.errors import TornAPIError
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.config.guild_config import GuildConfigService
    from app.discord.bot import TornWarIntelBot

logger = get_logger(__name__)


async def _torn_key_owner(key: str, base_url: str) -> tuple[bool, str]:
    """Return (ok, message). On success ``message`` is the key owner's name."""
    client = TornClient(api_key=key, base_url=base_url, timeout=10.0)
    try:
        body = await client.get("/user?selections=profile")
    except TornAPIError as exc:
        return False, str(exc)
    finally:
        await client.aclose()
    profile = body.get("profile") if isinstance(body, dict) else None
    if not isinstance(profile, dict) or "name" not in profile:
        return False, "Torn returned an unexpected response for this key."
    return True, str(profile["name"])


async def _faction_name(key: str, base_url: str, faction_id: int) -> tuple[bool, str]:
    client = TornClient(api_key=key, base_url=base_url, timeout=10.0)
    try:
        body = await client.get(f"/faction/{faction_id}/basic")
    except TornAPIError as exc:
        return False, str(exc)
    finally:
        await client.aclose()
    basic = body.get("basic") if isinstance(body, dict) else None
    if not isinstance(basic, dict) or "name" not in basic:
        return False, f"No faction found with ID {faction_id}."
    return True, str(basic["name"])


_POST_PERMISSIONS = (
    ("view_channel", "View Channel"),
    ("send_messages", "Send Messages"),
    ("embed_links", "Embed Links"),
    # fetch_message (used to edit the live boards) needs this.
    ("read_message_history", "Read Message History"),
)


def missing_post_permissions(perms: discord.Permissions) -> list[str]:
    """Names of the permissions the bot needs to keep live boards in a channel but lacks."""
    return [label for attr, label in _POST_PERMISSIONS if not getattr(perms, attr)]


# purpose -> what admins see. Order follows CHANNEL_LAYOUT.
BOARD_LABELS = {
    "information": "War information (war details + live score)",
    "stats": "Enemy stats",
    "targets": "Target board",
    "log": "War log",
    "activity": "Enemy activity",
}
_BOARD_CHOICES = [app_commands.Choice(name=BOARD_LABELS[p], value=p) for p, _, _ in CHANNEL_LAYOUT]
# The bot's own channel names minus the emoji: war-information, enemy-stats, ...
_BOARD_BY_NAME = {name.split("-", 1)[1]: purpose for purpose, name, _ in CHANNEL_LAYOUT}


def board_for_channel_name(name: str) -> str | None:
    """Which board a channel is for, judged by its name (``war-log``, ``📝-war-log``…)."""
    for suffix, purpose in _BOARD_BY_NAME.items():
        if name == suffix or name.endswith(f"-{suffix}"):
            return purpose
    return None


def register_setup(bot: TornWarIntelBot) -> None:
    base_url = bot.settings.torn_api_base_url
    setup = app_commands.Group(
        name="setup",
        description="Configure this server for war intelligence (admins only).",
        guild_only=True,
    )

    def ctx(interaction: discord.Interaction) -> tuple[int, int]:
        """(guild_id, invoking_user_id) — safe because the group is guild_only."""
        assert interaction.guild_id is not None
        return interaction.guild_id, interaction.user.id

    def cfg() -> GuildConfigService:
        assert bot.services is not None, "bot.services not attached"
        return bot.services.guild_config

    @setup.command(
        name="torn-key", description="Set this server's Torn API key (validated, kept private)."
    )
    @app_commands.describe(key="A Limited-access Torn API key from a faction member.")
    @requires_admin()
    async def setup_torn_key(interaction: discord.Interaction, key: str) -> None:
        guild_id, user_id = ctx(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await _torn_key_owner(key.strip(), base_url)
        if not ok:
            await interaction.followup.send(
                embed=base_embed("Torn key rejected", description=detail), ephemeral=True
            )
            return
        await cfg().set_torn_key(guild_id, key.strip(), by=user_id)
        await interaction.followup.send(
            embed=base_embed(
                "Torn key saved",
                description=f"Validated — key owner is **{detail}**. Stored encrypted.",
            ),
            ephemeral=True,
        )

    @setup.command(name="faction", description="Set which Torn faction this server tracks.")
    @app_commands.describe(faction_id="Your faction's numeric Torn ID.")
    @requires_admin()
    async def setup_faction(interaction: discord.Interaction, faction_id: int) -> None:
        guild_id, user_id = ctx(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved = await cfg().resolve(guild_id)
        name_hint = ""
        if resolved is not None:
            ok, detail = await _faction_name(resolved.torn_api_key, base_url, faction_id)
            if not ok:
                await interaction.followup.send(
                    embed=base_embed("Faction not set", description=detail), ephemeral=True
                )
                return
            name_hint = f" (**{detail}**)"
        await cfg().set_faction(guild_id, faction_id, by=user_id)
        tail = "" if resolved is not None else "\nNext: run `/setup torn-key`."
        await interaction.followup.send(
            embed=base_embed(
                "Faction saved",
                description=f"Tracking faction `{faction_id}`{name_hint}.{tail}",
            ),
            ephemeral=True,
        )

    @setup.command(
        name="ffscouter-key",
        description="Set this server's FFScouter key (optional — enables strength estimates).",
    )
    @app_commands.describe(key="16-character FFScouter key, or 'clear' to remove it.")
    @requires_admin()
    async def setup_ffscouter_key(interaction: discord.Interaction, key: str) -> None:
        guild_id, user_id = ctx(interaction)
        value = None if key.strip().lower() == "clear" else key.strip()
        await cfg().set_ffscouter_key(guild_id, value, by=user_id)
        msg = "FFScouter key removed." if value is None else "FFScouter key saved (encrypted)."
        await interaction.response.send_message(
            embed=base_embed("Updated", description=msg), ephemeral=True
        )

    @setup.command(name="role", description="Restrict war-intel commands to a role (optional).")
    @app_commands.describe(role="Role allowed to use war-intel commands. Omit to clear.")
    @requires_admin()
    async def setup_role(
        interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        guild_id, user_id = ctx(interaction)
        await cfg().set_role(guild_id, role.id if role else None, by=user_id)
        msg = (
            "Cleared — 'Manage Server' is now required for war-intel commands."
            if role is None
            else f"War-intel commands now also allowed for {role.mention}."
        )
        await interaction.response.send_message(
            embed=base_embed("Updated", description=msg), ephemeral=True
        )

    @setup.command(
        name="channel",
        description="Post everything into one existing channel instead of per-war channels.",
    )
    @app_commands.describe(
        channel="Existing text channel for all war content. Omit to go back to "
        "auto-created per-war channels."
    )
    @requires_admin()
    async def setup_channel(
        interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        guild_id, user_id = ctx(interaction)
        if channel is not None:
            # Discord lets an admin pick a channel the bot is hidden from; accepting it
            # silently moved every board there and each post failed with 403 Missing
            # Access (Null & Void, 29 Sep 2026). Refuse up front instead.
            missing = missing_post_permissions(channel.permissions_for(channel.guild.me))
            if missing:
                await interaction.response.send_message(
                    embed=base_embed(
                        "Can't use that channel",
                        description=(
                            f"I'm missing **{', '.join(missing)}** in {channel.mention}. "
                            "Give my role those permissions there (channel settings -> "
                            "Permissions), then run this again. Nothing was changed."
                        ),
                    ),
                    ephemeral=True,
                )
                return
        await cfg().set_war_room_channel(guild_id, channel.id if channel else None, by=user_id)
        msg = (
            "Cleared — new wars will get their own auto-created category and channels again."
            if channel is None
            else f"All war content will now post into {channel.mention}."
        )
        await interaction.response.send_message(
            embed=base_embed("Updated", description=msg), ephemeral=True
        )

    @setup.command(
        name="board",
        description="Run inside a channel you made to put one war board in it.",
    )
    @app_commands.describe(
        board="Which board goes here. Leave empty to go by the channel's name.",
        remove="Stop posting this board here.",
    )
    @app_commands.choices(board=_BOARD_CHOICES)
    @requires_admin()
    async def setup_board(
        interaction: discord.Interaction,
        board: app_commands.Choice[str] | None = None,
        remove: bool = False,
    ) -> None:
        guild_id, user_id = ctx(interaction)
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=base_embed(
                    "Can't use this here", description="Run this in a normal text channel."
                ),
                ephemeral=True,
            )
            return
        purpose = board.value if board is not None else board_for_channel_name(channel.name)
        if purpose is None:
            names = ", ".join(f"`{n}`" for n in _BOARD_BY_NAME)
            await interaction.response.send_message(
                embed=base_embed(
                    "Which board?",
                    description=(
                        f"I can't tell from the name {channel.mention}. Pick one with the "
                        f"`board` option, or name the channel one of: {names}."
                    ),
                ),
                ephemeral=True,
            )
            return
        label = BOARD_LABELS[purpose]

        if remove:
            await cfg().set_board_channel(guild_id, purpose, None, by=user_id)
            msg = f"**{label}** will no longer post here."
        else:
            # Same guard as /setup channel: a channel the bot can't post in would
            # silently fail every refresh.
            perms = channel.permissions_for(channel.guild.me)
            missing = missing_post_permissions(perms)
            if missing:
                await interaction.response.send_message(
                    embed=base_embed(
                        "Can't use this channel",
                        description=(
                            f"I'm missing **{', '.join(missing)}** here. Give my role those "
                            "permissions in this channel's settings, then run this again. "
                            "Nothing was changed."
                        ),
                    ),
                    ephemeral=True,
                )
                return
            await cfg().set_board_channel(guild_id, purpose, channel.id, by=user_id)
            msg = f"This channel now shows **{label}**. It appears within a minute during a war."
            if purpose == "log" and not perms.attach_files:
                msg += "\nTip: give me **Attach Files** here to get the end-of-war spreadsheet."

        boards = await cfg().board_channels(guild_id)
        todo = [BOARD_LABELS[p] for p, _, _ in CHANNEL_LAYOUT if p not in boards]
        if boards and todo:
            msg += "\n\nStill to set up: " + ", ".join(todo) + "."
        elif boards:
            msg += "\n\nAll 5 boards have a channel. I won't create any channels here."
        await interaction.response.send_message(
            embed=base_embed("Updated", description=msg), ephemeral=True
        )

    @setup.command(name="show", description="Show this server's configuration (keys redacted).")
    @requires_admin()
    async def setup_show(interaction: discord.Interaction) -> None:
        guild_id, _ = ctx(interaction)
        info = await cfg().describe(guild_id)
        embed = base_embed("Server configuration")
        for name, value in info.items():
            embed.add_field(name=name.replace("_", " "), value=value, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @setup.command(name="disable", description="Turn off war detection for this server.")
    @requires_admin()
    async def setup_disable(interaction: discord.Interaction) -> None:
        guild_id, _ = ctx(interaction)
        existed = await cfg().disable(guild_id)
        msg = "War detection disabled." if existed else "This server was not configured."
        await interaction.response.send_message(
            embed=base_embed("Updated", description=msg), ephemeral=True
        )

    bot.tree.add_command(setup)
