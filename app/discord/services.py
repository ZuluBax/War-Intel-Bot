"""Service container handed to the Discord bot.

Slash-command handlers need read access to the database, the intelligence layer
and per-guild configuration. ``main.py`` builds this and assigns it to
``bot.services`` before the bot connects; command code reads ``bot.services``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config.guild_config import GuildConfigService
from app.database.database import Database
from app.war.history import WarHistoryService
from app.war.intelligence import IntelligenceService

if TYPE_CHECKING:
    # Deferred: app.war.multi_guild imports app.discord.gateway, and this module
    # is imported by app.discord.bot (via app.discord's package __init__), so an
    # eager import here is circular. Type-checking only; no runtime import needed.
    from app.war.multi_guild import MultiGuildWarRunner


@dataclass(frozen=True)
class BotServices:
    database: Database
    intelligence: IntelligenceService
    guild_config: GuildConfigService
    runner: MultiGuildWarRunner
    war_history: WarHistoryService
