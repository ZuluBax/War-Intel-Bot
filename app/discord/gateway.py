"""A narrow interface over a Discord guild.

``WarChannelManager`` / ``WarBoard`` depend on :class:`GuildGateway`, not on
``discord.Guild`` directly, so their create-once / edit-in-place logic is unit
tested with :class:`tests.fake_gateway.FakeGuildGateway` and no live Discord.

The real implementation resolves the guild lazily on every call; before the bot
has connected it raises :class:`GatewayNotReady`, which callers treat as
"try again next tick" rather than an error.
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import discord

from app.utils.logging import get_logger

if TYPE_CHECKING:
    from discord.ext import commands


logger = get_logger(__name__)


class GatewayNotReady(RuntimeError):
    """The bot is not connected / cannot see the configured guild yet."""


@runtime_checkable
class GuildGateway(Protocol):
    async def category_exists(self, category_id: int) -> bool: ...

    async def channel_exists(self, channel_id: int) -> bool: ...

    async def can_manage_channels(self) -> bool:
        """Whether the bot holds Manage Channels server-wide. Read from cache — no API call."""
        ...

    async def create_category(self, name: str) -> int: ...

    async def rename_category(self, category_id: int, name: str) -> None:
        """Cosmetic: never raises for a Discord-side failure."""
        ...

    async def create_text_channel(self, name: str, *, category_id: int, topic: str) -> int: ...

    async def send_embeds(
        self,
        channel_id: int,
        embeds: list[discord.Embed],
        *,
        attachments: Sequence[tuple[str, bytes]] = (),
        view: discord.ui.View | None = None,
    ) -> int:
        """Post a message; ``attachments`` are ``(filename, content)`` files and
        ``view`` holds any buttons / drop-downs under it."""
        ...

    async def edit_embeds(
        self,
        channel_id: int,
        message_id: int,
        embeds: list[discord.Embed],
        *,
        view: discord.ui.View | None = None,
    ) -> bool:
        """Edit an existing message. Returns False if the message is gone.

        ``view=None`` leaves the message's buttons / drop-downs as they are; an empty
        ``discord.ui.View()`` removes them."""
        ...

    async def remove_message(self, channel_id: int, message_id: int) -> None:
        """Delete one of the bot's own messages; one already gone is not an error."""
        ...


class DiscordGuildGateway:
    """`GuildGateway` backed by a live ``discord.ext.commands.Bot`` + guild id."""

    def __init__(self, bot: commands.Bot, guild_id: int) -> None:
        self._bot = bot
        self._guild_id = guild_id

    def _guild(self) -> discord.Guild:
        guild = self._bot.get_guild(self._guild_id)
        if guild is None:
            raise GatewayNotReady(f"guild {self._guild_id} not available yet")
        return guild

    async def category_exists(self, category_id: int) -> bool:
        return isinstance(self._guild().get_channel(category_id), discord.CategoryChannel)

    async def channel_exists(self, channel_id: int) -> bool:
        return isinstance(self._guild().get_channel(channel_id), discord.TextChannel)

    async def can_manage_channels(self) -> bool:
        return self._guild().me.guild_permissions.manage_channels

    async def create_category(self, name: str) -> int:
        category = await self._guild().create_category(name)
        return category.id

    async def rename_category(self, category_id: int, name: str) -> None:
        category = self._guild().get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel) or category.name == name:
            return
        try:
            await category.edit(name=name)
        except discord.HTTPException as exc:  # a stale name is not worth failing a war over
            logger.warning(
                "gateway.category_rename_failed", category_id=category_id, detail=str(exc)
            )

    async def create_text_channel(self, name: str, *, category_id: int, topic: str) -> int:
        guild = self._guild()
        category = guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            raise GatewayNotReady(f"category {category_id} not available")
        # Copy the category's permissions explicitly: the owner locks the war category by
        # hand (the bot has no Manage Roles), and Discord's docs don't promise a new
        # channel inherits that lock. Setting overwrites at creation needs no Manage
        # Roles — only that the bot itself holds every permission they allow/deny.
        # Trimmed to what the bot holds: the live lock (checked 22 Sep 2026) also denies
        # TTS / voice messages, which the bot lacks, and Discord would refuse the whole
        # copy. Dropping those changes nothing — both already need Send Messages, and
        # "@everyone: no Send Messages" is kept.
        held = guild.me.guild_permissions.value
        overwrites = {
            target: discord.PermissionOverwrite.from_pair(
                discord.Permissions(allow.value & held), discord.Permissions(deny.value & held)
            )
            for target, (allow, deny) in ((t, o.pair()) for t, o in category.overwrites.items())
        }
        try:
            channel = await guild.create_text_channel(
                name, category=category, topic=topic, overwrites=overwrites
            )
        except discord.Forbidden as exc:
            # e.g. a mod added a permission the bot lacks: still create the channel,
            # unlocked, and say so — the owner can sync it to the category by hand.
            logger.warning(
                "gateway.channel_lock_not_copied",
                channel_name=name,
                category_id=category_id,
                detail=str(exc),
            )
            channel = await guild.create_text_channel(name, category=category, topic=topic)
        return channel.id

    async def send_embeds(
        self,
        channel_id: int,
        embeds: list[discord.Embed],
        *,
        attachments: Sequence[tuple[str, bytes]] = (),
        view: discord.ui.View | None = None,
    ) -> int:
        channel = self._guild().get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise GatewayNotReady(f"channel {channel_id} not available")
        files = [discord.File(io.BytesIO(data), filename=name) for name, data in attachments]
        # discord.py's typing wants a real View, so only pass the argument when there is one.
        extra: dict[str, Any] = {"view": view} if view is not None else {}
        try:
            if files:
                message = await channel.send(embeds=embeds, files=files, **extra)
            else:
                message = await channel.send(embeds=embeds, **extra)
        except discord.Forbidden:
            if not files:
                raise
            # A server invited before Attach Files was requested lacks it. The embeds
            # matter more than the file, so post them alone rather than lose both.
            # (Missing Send Messages fails again here and propagates as before.)
            logger.warning("gateway.attach_files_denied", channel_id=channel_id)
            message = await channel.send(embeds=embeds, **extra)
        return message.id

    async def edit_embeds(
        self,
        channel_id: int,
        message_id: int,
        embeds: list[discord.Embed],
        *,
        view: discord.ui.View | None = None,
    ) -> bool:
        channel = self._guild().get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise GatewayNotReady(f"channel {channel_id} not available")
        # A partial message edits without fetching first — one Discord call, not two.
        message = channel.get_partial_message(message_id)
        try:
            if view is None:
                await message.edit(embeds=embeds)
            else:
                await message.edit(embeds=embeds, view=view)
        except discord.NotFound:
            return False
        return True

    async def remove_message(self, channel_id: int, message_id: int) -> None:
        channel = self._guild().get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise GatewayNotReady(f"channel {channel_id} not available")
        with contextlib.suppress(discord.NotFound):  # deleted by hand already: same result
            await channel.get_partial_message(message_id).delete()
