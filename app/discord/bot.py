"""Discord bot bootstrap.

Multi-tenant: the bot is invited to any server via its OAuth2 link and
configured there with ``/setup``. There is no fixed guild id. Slash commands are
synced globally (so they show up in servers we haven't joined yet — can take up
to an hour to propagate) and, additionally, instantly per-server on ``on_ready``
and ``on_guild_join``.

Gateway intents: only ``guilds``. No privileged ``members`` / ``message_content``
intents — all enemy data comes from the Torn API, and everything is slash
commands.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.config.settings import Settings
from app.discord import alert_emoji
from app.discord.abroad_picker import handle_abroad_pick
from app.discord.services import BotServices
from app.utils.logging import get_logger

logger = get_logger(__name__)


class TornWarIntelBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self._dev_guild_id = settings.discord_dev_guild_id
        self._global_synced = False
        self._synced_guilds: set[int] = set()
        self._emoji_installed = False
        # Assigned by main.py before connect(); commands read it.
        self.services: BotServices | None = None

    async def setup_hook(self) -> None:
        from app.discord.commands import register_commands

        register_commands(self)

    async def on_ready(self) -> None:
        user = self.user
        logger.info(
            "discord.ready",
            bot_user=str(user) if user else None,
            bot_id=user.id if user else None,
            guilds=len(self.guilds),
        )
        if not self._emoji_installed:
            # on_ready fires again after every reconnect; one upload check is enough.
            self._emoji_installed = True
            await alert_emoji.install(self)
        await self._sync_commands()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info("discord.guild_joined", guild_id=guild.id, name=guild.name)
        await self._sync_guild(guild.id)

    async def _sync_commands(self) -> None:
        if not self._global_synced:
            try:
                await self.tree.sync()
                self._global_synced = True
                logger.info("discord.global_commands_synced")
            except discord.HTTPException as exc:
                logger.warning("discord.global_sync_failed", detail=str(exc))

        target_ids = {g.id for g in self.guilds}
        if self._dev_guild_id is not None:
            target_ids.add(self._dev_guild_id)
        for guild_id in target_ids:
            await self._sync_guild(guild_id)

    async def _sync_guild(self, guild_id: int) -> None:
        if guild_id in self._synced_guilds:
            return
        obj = discord.Object(id=guild_id)
        try:
            self.tree.copy_global_to(guild=obj)
            synced = await self.tree.sync(guild=obj)
        except discord.Forbidden:
            logger.error(
                "discord.command_sync_forbidden",
                guild_id=guild_id,
                fix=(
                    "Discord returned 403 on command sync — the bot was added without "
                    "the 'applications.commands' OAuth2 scope. Re-invite it with both "
                    "'bot' and 'applications.commands' ticked."
                ),
            )
            return
        except discord.HTTPException as exc:
            logger.warning("discord.guild_sync_failed", guild_id=guild_id, detail=str(exc))
            return
        self._synced_guilds.add(guild_id)
        logger.info("discord.guild_commands_synced", guild_id=guild_id, count=len(synced))

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Slash commands are routed by ``self.tree``; this only picks up the
        board's country drop-down, which has no registered view (see abroad_picker)."""
        try:
            await handle_abroad_pick(self, interaction)
        except Exception as exc:  # a failed private reply must never take the bot down
            logger.warning("abroad_picker.failed", error_type=type(exc).__name__, detail=str(exc))
            message = "Could not show that country — try again in a moment."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except discord.HTTPException:
                pass  # the interaction expired; the warning above is already logged

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.warning(
            "discord.command_error",
            command=interaction.command.name if interaction.command else None,
            error_type=type(error).__name__,
            detail=str(error),
        )
        message = "Something went wrong handling that command. It has been logged."
        if isinstance(error, app_commands.CheckFailure):
            message = "You do not have permission to use that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def build_bot(settings: Settings) -> TornWarIntelBot:
    return TornWarIntelBot(settings)
