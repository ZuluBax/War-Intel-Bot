from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.database.models import WarHit
from app.discord.hit_embeds import (
    CHAIN_MILESTONES,
    MILESTONE_COLOUR,
    OUTSIDE_HIT_COLOUR,
    WAR_HIT_COLOUR,
    build_hit_messages,
    hit_line,
)

T0 = datetime(2026, 9, 19, 18, 0, 0, tzinfo=UTC)  # 19:00 in London (BST)


def _hit(
    hit_id: int,
    offset_s: int,
    *,
    war: bool = True,
    attacker: str | None = "Alice",
    defender: str | None = "Bob",
    faction: str | None = "Rival Crew",
    result: str = "Attacked",
    respect: float | None = 4.68,
    warlord: float | None = None,
    chain: int | None = None,
) -> WarHit:
    return WarHit(
        id=hit_id,
        war_id=1,
        torn_attack_id=hit_id,
        started_at=T0 + timedelta(seconds=offset_s),
        attacker_name=attacker,
        defender_name=defender,
        defender_faction_name=faction,
        result=result,
        respect_gain=respect,
        is_war_hit=war,
        warlord=warlord,
        chain=chain,
        is_outgoing=True,
    )


def test_war_hits_are_green_and_outside_hits_are_orange() -> None:
    [message] = build_hit_messages([_hit(1, 0, war=True), _hit(2, 5, war=False)])
    war_embed, outside_embed = message.embeds

    assert war_embed.colour == WAR_HIT_COLOUR
    assert outside_embed.colour == OUTSIDE_HIT_COLOUR
    assert WAR_HIT_COLOUR != OUTSIDE_HIT_COLOUR
    assert war_embed.title is not None and "War hits" in war_embed.title
    assert outside_embed.title is not None and "Outside hits" in outside_embed.title


def test_consecutive_hits_of_one_kind_share_a_single_box() -> None:
    [message] = build_hit_messages([_hit(i, i) for i in range(1, 6)])
    assert len(message.embeds) == 1
    assert message.embeds[0].title == "🟢 War hits · 5"
    assert message.hit_ids == [1, 2, 3, 4, 5]


def test_time_order_is_kept_across_kinds_whatever_order_they_arrive_in() -> None:
    hits = [_hit(3, 30, war=True), _hit(1, 10, war=True), _hit(2, 20, war=False)]
    [message] = build_hit_messages(hits)

    assert [e.colour for e in message.embeds] == [
        WAR_HIT_COLOUR,
        OUTSIDE_HIT_COLOUR,
        WAR_HIT_COLOUR,
    ]
    assert message.hit_ids == [1, 2, 3]


def test_line_shows_london_time_names_result_and_respect() -> None:
    line = hit_line(_hit(1, 8, attacker="The_eVagabond", defender="NoZuaL"))
    assert line == r"`19:00:08` **The\_eVagabond** ➜ **NoZuaL** · Attacked · +4.68"


def test_outside_hit_names_the_faction_it_landed_on_but_a_war_hit_does_not() -> None:
    outside = hit_line(_hit(1, 0, war=False, faction="Agents of Truth"))
    war = hit_line(_hit(2, 0, war=True, faction="Rival Crew"))
    assert "Agents of Truth" in outside
    assert "Rival Crew" not in war


def test_line_copes_with_missing_names_and_no_respect() -> None:
    line = hit_line(_hit(1, 0, attacker=None, defender=None, faction=None, respect=None))
    assert "**Unknown** ➜ **Unknown**" in line
    assert "+" not in line


def test_markdown_in_names_cannot_break_formatting() -> None:
    line = hit_line(_hit(1, 0, attacker="**bold**", defender="_it_"))
    assert r"**\*\*bold\*\***" in line
    assert r"\_it\_" in line


def test_a_long_backlog_is_split_within_discords_limits() -> None:
    hits = [_hit(i, i, faction="A Rather Long Faction Name Ltd") for i in range(1, 401)]
    messages = build_hit_messages(hits)

    assert len(messages) > 1
    for message in messages:
        assert len(message.embeds) <= 10
        assert sum(len(e) for e in message.embeds) <= 6000
        for embed in message.embeds:
            assert embed.description is not None and len(embed.description) <= 4096
    # Every hit lands in exactly one message, in order.
    assert [i for m in messages for i in m.hit_ids] == list(range(1, 401))


def test_alternating_kinds_never_exceed_ten_boxes_per_message() -> None:
    hits = [_hit(i, i, war=i % 2 == 0) for i in range(1, 41)]
    messages = build_hit_messages(hits)
    assert all(len(m.embeds) <= 10 for m in messages)
    assert sum(len(m.embeds) for m in messages) == 40
    assert [i for m in messages for i in m.hit_ids] == list(range(1, 41))


def test_no_hits_no_messages() -> None:
    assert build_hit_messages([]) == []


def test_line_says_yes_when_a_ranked_war_weapon_warlord_bonus_was_used() -> None:
    assert hit_line(_hit(1, 0, warlord=1.15)).endswith("+4.68 · RW: yes")
    assert hit_line(_hit(2, 0, warlord=1.18)).endswith("+4.68 · RW: yes")


def test_line_says_no_when_there_was_no_warlord_bonus() -> None:
    assert hit_line(_hit(1, 0, warlord=1.0)).endswith("+4.68 · RW: no")


def test_rw_does_not_depend_on_whether_it_was_a_war_hit() -> None:
    # colour (war vs outside) and RW (weapon) are independent facts
    assert hit_line(_hit(1, 0, war=True, warlord=1.0)).endswith("RW: no")
    assert hit_line(_hit(2, 0, war=False, warlord=1.15)).endswith("RW: yes")


def test_a_hit_stored_before_the_bonus_was_kept_shows_no_rw_text_yet() -> None:
    assert "RW" not in hit_line(_hit(1, 0, warlord=None))


def test_line_shows_the_chain_number_for_a_chain_hit() -> None:
    assert hit_line(_hit(1, 0, chain=380)).endswith("+4.68 · Chain 380")
    assert "Chain" not in hit_line(_hit(2, 0, chain=0))  # not a chain hit (a loss, an assist)
    assert "Chain" not in hit_line(_hit(3, 0, chain=None))  # stored before the chain was kept


def test_chain_comes_before_the_rw_label() -> None:
    assert hit_line(_hit(1, 0, chain=380, warlord=1.15)).endswith("+4.68 · Chain 380 · RW: yes")


def test_the_users_milestones_are_all_recognised_and_ordinary_numbers_are_not() -> None:
    for number in (10, 25, 50, 100, 250, 500, 1000, 2500, 5000):
        assert hit_line(_hit(1, 0, chain=number)).count("🏆") == 1, number
    for number in (1, 9, 11, 24, 26, 49, 99, 101, 249, 499, 999, 1001):
        assert "🏆" not in hit_line(_hit(1, 0, chain=number)), number
    assert {10000, 25000, 50000, 100000} <= CHAIN_MILESTONES  # "ect": the pattern goes on


def test_a_milestone_line_is_bold_and_uses_thousands_separators() -> None:
    assert hit_line(_hit(1, 0, chain=100)).endswith("🏆 **CHAIN 100**")
    assert hit_line(_hit(2, 0, chain=2500)).endswith("🏆 **CHAIN 2,500**")


def test_a_milestone_hit_gets_its_own_gold_box_between_the_green_ones() -> None:
    hits = [_hit(1, 1, chain=98), _hit(2, 2, chain=99), _hit(3, 3, chain=100), _hit(4, 4, chain=101)]
    [message] = build_hit_messages(hits)
    before, milestone, after = message.embeds

    assert [e.colour for e in message.embeds] == [WAR_HIT_COLOUR, MILESTONE_COLOUR, WAR_HIT_COLOUR]
    assert before.title == "🟢 War hits · 2"
    assert milestone.title == "🏆 CHAIN 100 · war hit"
    assert milestone.description is not None and milestone.description.count("\n") == 0
    assert after.title == "🟢 War hits · 1"
    assert message.hit_ids == [1, 2, 3, 4]  # nothing lost, order kept


def test_an_outside_milestone_says_so_and_two_in_a_row_stay_separate() -> None:
    hits = [_hit(1, 1, war=False, chain=10), _hit(2, 2, war=False, chain=50)]
    [message] = build_hit_messages(hits)

    assert [e.title for e in message.embeds] == [
        "🏆 CHAIN 10 · outside hit",
        "🏆 CHAIN 50 · outside hit",
    ]
    assert message.hit_ids == [1, 2]


def test_many_milestones_still_respect_discords_limits() -> None:
    hits = [_hit(i, i, chain=100) for i in range(1, 60)]
    messages = build_hit_messages(hits)
    assert all(len(m.embeds) <= 10 for m in messages)
    assert [i for m in messages for i in m.hit_ids] == list(range(1, 60))
