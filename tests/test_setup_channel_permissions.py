from __future__ import annotations

import discord

from app.discord.setup_commands import missing_post_permissions


def test_hidden_channel_is_refused_with_every_missing_permission_named() -> None:
    assert missing_post_permissions(discord.Permissions.none()) == [
        "View Channel",
        "Send Messages",
        "Embed Links",
        "Read Message History",
    ]


def test_channel_with_posting_permissions_is_accepted() -> None:
    perms = discord.Permissions(
        view_channel=True, send_messages=True, embed_links=True, read_message_history=True
    )
    assert missing_post_permissions(perms) == []


def test_missing_only_history_is_reported() -> None:
    perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    assert missing_post_permissions(perms) == ["Read Message History"]
