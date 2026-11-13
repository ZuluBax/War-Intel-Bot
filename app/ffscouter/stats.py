"""Higher-level stat-estimate helpers built on :class:`FFScouterClient`.

PHASE 1: skeleton. Phase 3 wires this to the database: fetch a roster's
estimates, append a :class:`app.database.models.StatEstimate` row per player
(append-only history), and never overwrite the last good estimate when a refresh
fails (spec §27).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.ffscouter.client import FFScouterClient
from app.ffscouter.models import FFScouterStat


class StatEstimateService:
    def __init__(self, client: FFScouterClient) -> None:
        self._client = client

    async def fetch_for_players(self, player_ids: Sequence[int]) -> dict[int, FFScouterStat]:
        """Fetch current estimates. Thin pass-through for now; Phase 3 adds
        persistence + history + confidence derivation."""
        return await self._client.get_stats(player_ids)
