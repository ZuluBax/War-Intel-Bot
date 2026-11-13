"""Slash-command permission checks (spec §25, §37).

Sensitive war-intelligence commands are gated by:

1. the Discord "Manage Server" permission (always allowed), or
2. a role configured per server via ``/setup role`` (stored in ``guild_configs``).

``/setup`` itself always requires "Manage Server" (or Administrator).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypeVar

import discord
from discord import app_commands

if TYPE_CHECKING:
    from app.discord.bot import TornWarIntelBot

_T = TypeVar("_T")


def _services(interaction: discord.Interaction) -> object | None:
    return getattr(interaction.client, "services", None)


async def _has_intel_access(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member) or interaction.guild_id is None:
        return False
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True
    services = _services(interaction)
    if services is None:
        return False
    bot: TornWarIntelBot = interaction.client  # type: ignore[assignment]
    assert bot.services is not None
    role_id = await bot.services.guild_config.war_intel_role_id(interaction.guild_id)
    return role_id is not None and any(r.id == role_id for r in member.roles)


def _is_admin(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and (
        member.guild_permissions.manage_guild or member.guild_permissions.administrator
    )


def requires_intel_access() -> Callable[[_T], _T]:
    return app_commands.check(_has_intel_access)


def requires_admin() -> Callable[[_T], _T]:
    return app_commands.check(_is_admin)
