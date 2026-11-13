"""Our-vs-their matchups (Phase 8): one of our players against every enemy.

Pure functions — no I/O. The comparison is a plain ratio of battle-strength numbers
(each a trusted spy total or an FFScouter estimate, app.war.spy.best_strength). It is
a rough guide, not a fight simulator: real outcomes also depend on the stat split,
weapons and armour, which no estimate captures.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.war.target_service import TargetView

# Enemy strength as a share of ours. Under GOOD_BELOW they are clearly weaker; over
# AVOID_ABOVE they are clearly stronger. The band between is a coin-flip zone where
# estimate error (often 10-20%) alone could swing the fight.
GOOD_BELOW = 0.75
AVOID_ABOVE = 1.25


@dataclass(frozen=True)
class Matchup:
    target: TargetView
    ratio: float  # enemy strength / ours


@dataclass(frozen=True)
class Matchups:
    good: list[Matchup]
    even: list[Matchup]
    avoid: list[Matchup]
    unknown: int  # enemies with no strength number at all


def build_matchups(our_strength: float, targets: Sequence[TargetView]) -> Matchups:
    """Sort every enemy into good / even / avoid against ``our_strength`` (> 0).

    Inside each group, enemies you can hit right now come first. Good fights are then
    strongest-first — the toughest enemy you can still beat is the best use of a hit —
    and the rest weakest-first, so the nearest-to-winnable fights lead.
    """
    if our_strength <= 0:
        raise ValueError("our_strength must be positive")
    good: list[Matchup] = []
    even: list[Matchup] = []
    avoid: list[Matchup] = []
    unknown = 0
    for t in targets:
        if t.estimated_bs is None:
            unknown += 1
            continue
        m = Matchup(target=t, ratio=t.estimated_bs / our_strength)
        if m.ratio < GOOD_BELOW:
            good.append(m)
        elif m.ratio > AVOID_ABOVE:
            avoid.append(m)
        else:
            even.append(m)
    good.sort(key=lambda m: (not m.target.is_available, -m.ratio))
    even.sort(key=lambda m: (not m.target.is_available, m.ratio))
    avoid.sort(key=lambda m: (not m.target.is_available, m.ratio))
    return Matchups(good=good, even=even, avoid=avoid, unknown=unknown)
