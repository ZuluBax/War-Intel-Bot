"""Resolve a user-supplied player reference (Torn ID or name) to a stored row."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Player


async def resolve_player(session: AsyncSession, query: str) -> Player | None:
    """Find a :class:`Player` by numeric Torn ID or (case-insensitive) name.

    Returns ``None`` if nothing matches. A name that matches several players
    returns the most recently updated one.
    """
    query = query.strip().lstrip("[").rstrip("]").strip()
    if not query:
        return None

    if query.isdigit():
        by_id: Player | None = await session.scalar(
            select(Player).where(Player.torn_player_id == int(query))
        )
        return by_id

    by_name: Player | None = await session.scalar(
        select(Player)
        .where(func.lower(Player.name) == query.lower())
        .order_by(Player.updated_at.desc())
        .limit(1)
    )
    return by_name
