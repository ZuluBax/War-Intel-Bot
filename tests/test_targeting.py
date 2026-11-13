from __future__ import annotations

from app.war.targeting import ALGORITHM_VERSION, TargetingWeights, TargetInput, rank


def test_weights_have_documented_defaults_summing_to_one() -> None:
    w = TargetingWeights()
    assert round(w.strength_weight + w.fair_fight_weight + w.level_weight, 6) == 1.0


def test_algorithm_version_is_pinned() -> None:
    assert ALGORITHM_VERSION == "v1"


def test_rank_of_empty_input_is_empty() -> None:
    assert rank([]) == []


def _target(
    pid: int,
    *,
    bs: float | None,
    ff: float | None = 1.0,
    level: int | None = 20,
    available: bool = True,
) -> TargetInput:
    return TargetInput(
        player_id=pid, estimated_bs=bs, fair_fight=ff, level=level, is_available=available
    )


def test_weaker_estimated_strength_ranks_higher() -> None:
    weak = _target(1, bs=1.0e9)
    strong = _target(2, bs=9.0e9)
    scores = rank([strong, weak])
    assert [s.player_id for s in scores] == [1, 2]
    assert scores[0].score > scores[1].score


def test_lower_fair_fight_ranks_higher() -> None:
    safe = _target(1, bs=5.0e9, ff=1.0)
    risky = _target(2, bs=5.0e9, ff=3.0)
    scores = rank([risky, safe])
    assert [s.player_id for s in scores] == [1, 2]


def test_lower_level_ranks_higher_all_else_equal() -> None:
    low = _target(1, bs=5.0e9, ff=1.0, level=10)
    high = _target(2, bs=5.0e9, ff=1.0, level=80)
    scores = rank([high, low])
    assert [s.player_id for s in scores] == [1, 2]


def test_unknown_strength_never_outranks_a_confirmed_weak_target() -> None:
    confirmed_weak = _target(1, bs=1.0e9)
    unknown = _target(2, bs=None)
    scores = rank([unknown, confirmed_weak])
    assert scores[0].player_id == 1


def test_unavailable_target_is_capped_at_low_regardless_of_score() -> None:
    weakest_but_unavailable = _target(1, bs=1.0e8, available=False)
    mediocre_available = _target(2, bs=5.0e9, available=True)
    scores = rank([weakest_but_unavailable, mediocre_available])

    by_id = {s.player_id: s for s in scores}
    assert by_id[1].priority == "LOW"
    # Availability gates priority, but the underlying score is preserved.
    assert by_id[1].score > by_id[2].score
    # Available targets always sort ahead of unavailable ones.
    assert [s.player_id for s in scores] == [2, 1]


def test_single_known_value_is_neutral_not_extreme() -> None:
    # Only one distinct BS value in the batch -> nothing to differentiate on.
    a = _target(1, bs=5.0e9)
    b = _target(2, bs=5.0e9)
    scores = rank([a, b])
    assert scores[0].score == scores[1].score


def test_priorities_use_all_three_bands() -> None:
    targets = [_target(i, bs=float(i) * 1.0e9) for i in range(1, 11)]
    priorities = {s.priority for s in rank(targets)}
    assert priorities == {"HIGH", "MEDIUM", "LOW"}


def test_custom_weights_change_the_ranking() -> None:
    # Weak strength but bad (high) fair fight vs. strong but great fair fight.
    a = _target(1, bs=1.0e9, ff=3.0)
    b = _target(2, bs=9.0e9, ff=1.0)

    strength_only = TargetingWeights(strength_weight=1.0, fair_fight_weight=0.0, level_weight=0.0)
    assert rank([a, b], weights=strength_only)[0].player_id == 1

    ff_only = TargetingWeights(strength_weight=0.0, fair_fight_weight=1.0, level_weight=0.0)
    assert rank([a, b], weights=ff_only)[0].player_id == 2
