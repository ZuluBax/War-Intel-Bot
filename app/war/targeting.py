"""Target-priority ranking (spec §17, Phase 6).

The scoring function is isolated here, pure and dependency-free, so it can be
replaced/improved later (and fuzz-tested in isolation) without touching the
rest of the app. The MVP algorithm is a deliberately simple, documented,
configurable weighting of:

* estimated enemy battle strength (lower = easier = higher priority),
* Fair Fight (lower FF = better reward-to-risk),
* player level (lower = presumed easier, weakest signal of the three).

Each factor is min-max normalised *within the batch passed to* :func:`rank` —
this is a relative "who's the easiest pick on THIS roster" ranking, not an
absolute strength scale, so it stays meaningful across very different wars.
A value the batch doesn't know (``None``) never scores as an easy pick — we
can't vouch for an unconfirmed target, so unknown never outranks confirmed.

Current status is deliberately **not** one of the three configurable weights:
an enemy who cannot be attacked right now (hospital/jail/travelling/unknown)
is always capped at ``LOW`` priority regardless of how weak they'd otherwise
score — attackability is a hard gate, not something a weight should be able
to outvote.

It is called TARGET PRIORITY, never BEST TARGET — it is a heuristic, not a
guarantee (spec §17).
"""

from __future__ import annotations

from dataclasses import dataclass

ALGORITHM_VERSION = "v1"

_HIGH_THRESHOLD = 0.66
_MEDIUM_THRESHOLD = 0.33
_PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


@dataclass(frozen=True)
class TargetingWeights:
    """Configurable weights. Persisted defaults documented in docs/targeting.md."""

    strength_weight: float = 0.6
    fair_fight_weight: float = 0.25
    level_weight: float = 0.15


@dataclass(frozen=True)
class TargetInput:
    player_id: int
    estimated_bs: float | None
    fair_fight: float | None
    level: int | None
    is_available: bool  # False if hospitalised / jailed / travelling / unknown-bad


@dataclass(frozen=True)
class TargetScore:
    player_id: int
    score: float
    priority: str  # HIGH / MEDIUM / LOW


def _component(value: float | None, known: list[float]) -> float:
    """Normalise ``value`` to [0, 1] within ``known``, inverted so *lower* raw
    values score *higher* (weaker/easier -> more attractive target).

    ``None`` (unconfirmed) scores 0 — never an easy pick. Fewer than two
    distinct known values means there's nothing to differentiate on, so every
    confirmed value gets a neutral 0.5 rather than an arbitrary 0 or 1.
    """
    if value is None:
        return 0.0
    lo, hi = min(known), max(known)
    if lo == hi:
        return 0.5
    return 1 - (value - lo) / (hi - lo)


def _bucket(score: float) -> str:
    if score >= _HIGH_THRESHOLD:
        return "HIGH"
    if score >= _MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def rank(
    targets: list[TargetInput], *, weights: TargetingWeights | None = None
) -> list[TargetScore]:
    """Return targets ordered by descending priority (HIGH first), then score."""
    if not targets:
        return []
    w = weights or TargetingWeights()

    known_bs = [t.estimated_bs for t in targets if t.estimated_bs is not None]
    known_ff = [t.fair_fight for t in targets if t.fair_fight is not None]
    known_lvl = [float(t.level) for t in targets if t.level is not None]

    def level_component(t: TargetInput) -> float:
        return _component(float(t.level) if t.level is not None else None, known_lvl)

    pairs: list[tuple[TargetInput, TargetScore]] = []
    for t in targets:
        raw = (
            w.strength_weight * _component(t.estimated_bs, known_bs)
            + w.fair_fight_weight * _component(t.fair_fight, known_ff)
            + w.level_weight * level_component(t)
        )
        priority = _bucket(raw) if t.is_available else "LOW"
        score = TargetScore(player_id=t.player_id, score=round(raw, 4), priority=priority)
        pairs.append((t, score))

    # Attackable targets always sort ahead of ones you can't act on right now
    # (hospital/jail/travelling/unknown), regardless of score.
    pairs.sort(key=lambda p: (not p[0].is_available, _PRIORITY_RANK[p[1].priority], -p[1].score))
    return [score for _, score in pairs]
