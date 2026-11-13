"""The live ranked-war score shown in the small box on ``#war-information``.

Pure — no I/O. The numbers come from the ``/rankedwars`` response the war check
already fetches every tick, so showing the score costs no extra Torn API calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.torn.models import RankedWar


@dataclass(frozen=True)
class WarScore:
    our_name: str | None
    enemy_name: str | None
    # None when Torn omitted the field — shown as "—", never as a fake 0.
    our_score: int | None
    enemy_score: int | None
    target_score: int | None
    finished: bool = False
    result: str | None = None  # WIN / LOSS / DRAW once Torn names a winner

    @property
    def lead(self) -> int | None:
        """Our points minus theirs (negative = we are behind); None if either is unknown."""
        if self.our_score is None or self.enemy_score is None:
            return None
        return self.our_score - self.enemy_score


def score_from_war(
    war: RankedWar, our_faction_id: int, *, finished: bool, result: str | None
) -> WarScore | None:
    """The score for ``war`` from our side, or None if either faction is missing
    from Torn's response (nothing sensible to show)."""
    us = next((f for f in war.factions if f.faction_id == our_faction_id), None)
    enemy = war.opponent_of(our_faction_id)
    if us is None or enemy is None:
        return None
    return WarScore(
        our_name=us.name,
        enemy_name=enemy.name,
        our_score=us.score,
        enemy_score=enemy.score,
        target_score=war.target_score,
        finished=finished,
        result=result,
    )
