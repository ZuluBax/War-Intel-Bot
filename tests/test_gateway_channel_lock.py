"""New war channels copy the (hand-locked) category's permissions."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec

import discord

from app.discord.gateway import DiscordGuildGateway

# The live server's lock (22 Sep 2026): @everyone denied send + TTS + voice messages;
# the bot role allowed send + TTS + threads + voice messages.
_LOCK = {
    "everyone": discord.PermissionOverwrite(
        send_messages=False, send_tts_messages=False, send_voice_messages=False
    ),
    "bot_role": discord.PermissionOverwrite(
        send_messages=True,
        send_tts_messages=True,
        send_messages_in_threads=True,
        send_voice_messages=True,
    ),
}
# The bot's own server permissions (invite bitmask 117776).
_BOT_HOLDS = discord.Permissions(117776)


def _gateway(*create_outcomes: Any) -> tuple[DiscordGuildGateway, Any]:
    category = create_autospec(discord.CategoryChannel, instance=True)
    category.overwrites = _LOCK
    guild = MagicMock()
    guild.me.guild_permissions = _BOT_HOLDS
    guild.get_channel.return_value = category
    guild.create_text_channel = AsyncMock(side_effect=list(create_outcomes))
    bot = MagicMock()
    bot.get_guild.return_value = guild
    return DiscordGuildGateway(bot, 1), guild


async def test_new_channel_gets_the_category_lock() -> None:
    gateway, guild = _gateway(SimpleNamespace(id=55))
    assert await gateway.create_text_channel("🕒-enemy-activity", category_id=9, topic="t") == 55
    copied = guild.create_text_channel.await_args.kwargs["overwrites"]
    # The part that matters survives: nobody but the bot can post.
    assert copied["everyone"].send_messages is False
    assert copied["bot_role"].send_messages is True
    # Bits the bot doesn't hold are dropped, or Discord would refuse the whole copy.
    for overwrite in copied.values():
        allow, deny = overwrite.pair()
        assert (allow.value | deny.value) & ~_BOT_HOLDS.value == 0


async def test_channel_still_created_if_the_lock_cannot_be_copied() -> None:
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing")
    gateway, guild = _gateway(forbidden, SimpleNamespace(id=56))
    assert await gateway.create_text_channel("🕒-enemy-activity", category_id=9, topic="t") == 56
    assert "overwrites" not in guild.create_text_channel.await_args.kwargs
