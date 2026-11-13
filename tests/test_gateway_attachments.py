"""``DiscordGuildGateway.send_embeds`` file handling, with a stubbed Discord channel."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, create_autospec

import discord
import pytest

from app.discord.gateway import DiscordGuildGateway

FILES = [("war-summary.csv", b"Player,Hits\nAlice,3\n")]


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


def _gateway(*outcomes: Any) -> tuple[DiscordGuildGateway, Any]:
    channel = create_autospec(discord.TextChannel, instance=True)
    channel.send.side_effect = list(outcomes)
    guild = MagicMock()
    guild.get_channel.return_value = channel
    bot = MagicMock()
    bot.get_guild.return_value = guild
    return DiscordGuildGateway(bot, 1), channel


async def test_sends_the_file_with_the_embeds() -> None:
    gateway, channel = _gateway(SimpleNamespace(id=7))
    assert await gateway.send_embeds(5, [discord.Embed(title="x")], attachments=FILES) == 7

    assert channel.send.call_count == 1
    [sent] = channel.send.call_args.kwargs["files"]
    assert sent.filename == "war-summary.csv"


async def test_a_server_without_attach_files_still_gets_the_embeds() -> None:
    gateway, channel = _gateway(_forbidden(), SimpleNamespace(id=8))
    assert await gateway.send_embeds(5, [discord.Embed(title="x")], attachments=FILES) == 8

    assert channel.send.call_count == 2
    assert channel.send.call_args_list[0].kwargs["files"]  # first try had the file
    assert "files" not in channel.send.call_args_list[1].kwargs  # retry did not


async def test_a_plain_forbidden_without_files_is_not_swallowed() -> None:
    gateway, channel = _gateway(_forbidden())
    with pytest.raises(discord.Forbidden):
        await gateway.send_embeds(5, [discord.Embed(title="x")])
    assert channel.send.call_count == 1


async def test_a_channel_the_bot_cannot_send_in_still_fails_loudly_even_with_files() -> None:
    gateway, channel = _gateway(_forbidden(), _forbidden())
    with pytest.raises(discord.Forbidden):
        await gateway.send_embeds(5, [discord.Embed(title="x")], attachments=FILES)
    assert channel.send.call_count == 2
