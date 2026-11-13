"""The animated warning emoji on the target board (medical cut / revive alerts).

Discord cannot make text blink, so the "flashing" comes from animated emoji. They are
**application emoji** — owned by the bot itself, not by any server — so they work in
every server the bot is in, without Nitro and without the "Use External Emojis"
permission (Discord docs, Emoji Resource). The GIFs ship in ``assets/`` and are
uploaded once on startup if the bot does not already own them.

Until the upload has worked (or if it fails), plain Unicode stand-ins are used, so the
board never shows a broken ``:name:``.
"""

from __future__ import annotations

from pathlib import Path

import discord

from app.utils.logging import get_logger

logger = get_logger(__name__)

_ASSETS = Path(__file__).parent / "assets"

# Discord emoji names: 2-32 characters, letters/digits/underscores. Prefixed so they
# can't clash with anything else the application might own later.
WARNING_NAME = "twi_warning_pulse"
SIREN_NAME = "twi_siren"


class AlertEmoji:
    """Current emoji strings; the board reads these at render time."""

    warning: str = "⚠️"
    siren: str = "🚨"


async def install(client: discord.Client) -> None:
    """Make sure the bot owns both animated emoji and point :class:`AlertEmoji` at them.
    Never raises: a failure only leaves the Unicode stand-ins in place."""
    try:
        owned = {e.name: e for e in await client.fetch_application_emojis()}
        for name, attr in ((WARNING_NAME, "warning"), (SIREN_NAME, "siren")):
            emoji = owned.get(name)
            if emoji is None:
                image = (_ASSETS / f"{name}.gif").read_bytes()
                emoji = await client.create_application_emoji(name=name, image=image)
                logger.info("alert_emoji.uploaded", name=name, emoji_id=emoji.id)
            setattr(AlertEmoji, attr, str(emoji))
        logger.info("alert_emoji.ready", warning=AlertEmoji.warning, siren=AlertEmoji.siren)
    except (discord.DiscordException, OSError) as exc:
        logger.warning("alert_emoji.unavailable", error_type=type(exc).__name__, detail=str(exc))
