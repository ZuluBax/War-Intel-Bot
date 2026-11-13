from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord

from app.discord.target_board import (
    abroad_countries,
    country_embed,
    target_board_cleared_embed,
    target_board_embed,
)
from app.torn.models import PlayerState
from app.war.target_service import TargetView

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _ts(when: datetime, style: str) -> str:
    return f"<t:{int(when.timestamp())}:{style}>"


def _view(
    i: int,
    *,
    state: PlayerState = PlayerState.OKAY,
    description: str | None = "Okay",
    until: datetime | None = None,
    plane: str | None = None,
    since: datetime | None = None,
    name: str | None = None,
    bs: float | None = 1.0e9,
    ff: float | None = 1.5,
    online: str | None = None,
) -> TargetView:
    return TargetView(
        player_id=1000 + i,
        name=name or f"Enemy{i}",
        level=10 + i,
        rank=i,
        score=1.0,
        priority="MEDIUM",
        state=state,
        is_available=state is PlayerState.OKAY,
        estimated_bs=bs,
        fair_fight=ff,
        status_description=description,
        status_until=until,
        plane_type=plane,
        status_since=since,
        online_status=online,
    )


def _text(embeds: list[discord.Embed]) -> str:
    """Everything the reader sees: the description, then any fields it continues in."""
    parts: list[str] = []
    for e in embeds:
        parts.append(e.description or "")
        parts.extend(f.value or "" for f in e.fields)
    return "\n".join(parts)


def _message_size(embeds: list[discord.Embed]) -> int:
    """Discord counts titles, descriptions and footers of every embed in a message."""
    return sum(
        len(e.title or "")
        + len(e.description or "")
        + len(e.footer.text or "")
        + sum(len(f.name or "") + len(f.value or "") for f in e.fields)
        for e in embeds
    )


# --- the list itself --------------------------------------------------------


def test_one_list_grouped_available_then_hospital_then_landing_then_abroad_then_out() -> None:
    views = [
        _view(1, state=PlayerState.TRAVELING, description="Traveling from Torn to Mexico",
              plane="airliner", since=_NOW - timedelta(minutes=10)),
        _view(2, state=PlayerState.TRAVELING, description="In Mexico"),
        _view(3, state=PlayerState.TRAVELING, description="Traveling from Japan to Torn",
              plane="airliner", since=_NOW - timedelta(minutes=10)),
        _view(4, state=PlayerState.HOSPITAL, description="In hospital", until=_NOW + timedelta(minutes=30)),
        _view(5),
    ]
    text = _text(target_board_embed(views, now=_NOW))
    order = [text.index(f"Enemy{i}") for i in (5, 4, 3, 2, 1)]
    assert order == sorted(order)


def test_available_targets_keep_the_priority_order() -> None:
    views = [_view(3), _view(1), _view(2)]  # rank == i
    text = _text(target_board_embed(views, now=_NOW))
    assert text.index("Enemy1") < text.index("Enemy2") < text.index("Enemy3")


def test_available_line_has_a_clickable_attack_link_and_others_do_not() -> None:
    views = [
        _view(1),
        _view(2, state=PlayerState.HOSPITAL, description="In hospital", until=_NOW + timedelta(hours=1)),
    ]
    text = _text(target_board_embed(views, now=_NOW))
    assert "https://www.torn.com/page.php?sid=attack&user2ID=1001" in text
    assert "loader.php" not in text  # Torn retired it: it returns an error page
    assert "user2ID=1002" not in text  # cannot be attacked from hospital


def test_shows_estimated_battle_stats_and_a_marker_when_unknown() -> None:
    known = _text(target_board_embed([_view(1, bs=1.0e9, ff=1.5)], now=_NOW))
    assert "1.00B" in known
    assert "FF 1.50" in known
    unknown = _text(target_board_embed([_view(1, bs=None, ff=None)], now=_NOW))
    assert "?" in unknown


def test_empty_roster_still_returns_one_embed() -> None:
    embeds = target_board_embed([], now=_NOW)
    assert len(embeds) == 1
    assert "No enemy roster" in embeds[0].description


def test_cleared_embed_says_the_war_is_over_not_that_the_roster_is_empty() -> None:
    embed = target_board_cleared_embed(now=_NOW)
    assert embed.title == "⚔️ TARGET BOARD"
    assert "war over" in (embed.description or "").lower()
    # Distinct wording from the empty-roster case, so a player can't mistake a just-
    # ended war for a not-yet-built roster.
    assert "No enemy roster" not in (embed.description or "")


def test_player_name_cannot_break_the_link_or_the_formatting() -> None:
    text = _text(target_board_embed([_view(1, name="Bad[Na]me_x")], now=_NOW))
    assert "Bad[" not in text
    assert "BadName" in text.replace("\\", "")


# --- hospital ---------------------------------------------------------------


def test_hospital_shows_a_live_countdown_and_the_clock_time() -> None:
    out = _NOW + timedelta(minutes=53)
    views = [_view(1, state=PlayerState.HOSPITAL, description="In hospital", until=out)]
    text = _text(target_board_embed(views, now=_NOW))
    assert f"out {_ts(out, 'R')} ({_ts(out, 't')})" in text


def test_hospital_sorts_soonest_release_first_and_unknown_last() -> None:
    views = [
        _view(1, state=PlayerState.HOSPITAL, description="In hospital", until=_NOW + timedelta(hours=3)),
        _view(2, state=PlayerState.HOSPITAL, description="In hospital", until=None),
        _view(3, state=PlayerState.HOSPITAL, description="In hospital", until=_NOW + timedelta(minutes=5)),
    ]
    text = _text(target_board_embed(views, now=_NOW))
    assert text.index("Enemy3") < text.index("Enemy1") < text.index("Enemy2")
    assert "out ?" in text


def test_hospital_time_already_passed_reads_out_now() -> None:
    views = [_view(1, state=PlayerState.HOSPITAL, description="In hospital", until=_NOW - timedelta(seconds=20))]
    assert "out now" in _text(target_board_embed(views, now=_NOW))


def test_jail_is_labelled_as_jail() -> None:
    views = [_view(1, state=PlayerState.JAIL, description="In jail", until=_NOW + timedelta(hours=1))]
    assert "out of jail" in _text(target_board_embed(views, now=_NOW))


# --- flights ----------------------------------------------------------------


def test_incoming_flight_shows_the_route_and_an_estimated_landing() -> None:
    left = _NOW - timedelta(minutes=60)
    views = [
        _view(1, state=PlayerState.TRAVELING, description="Traveling from Japan to Torn",
              plane="light_aircraft", since=left)
    ]
    text = _text(target_board_embed(views, now=_NOW))
    earliest = left - timedelta(seconds=60) + timedelta(minutes=225 * 0.645 - 1.5)
    latest = left + timedelta(minutes=225 * 0.692 + 1.5)
    assert "Japan → Torn" in text
    assert f"lands ~{_ts(earliest, 'R')} ({_ts(earliest, 't')}-{_ts(latest, 't')}) PI" in text
    assert "LANDING IN TORN" in text


def test_outgoing_flight_shows_the_route_and_an_estimated_landing() -> None:
    views = [
        _view(1, state=PlayerState.TRAVELING, description="Traveling from Torn to Mexico",
              plane="light_aircraft", since=_NOW - timedelta(minutes=2))
    ]
    text = _text(target_board_embed(views, now=_NOW))
    assert "🇲🇽 __**Mexico**__ (0 + 1 flying in)" in text
    assert "flying in · lands ~" in text
    assert "FLYING OUT" not in text  # listed under its destination instead


def test_flight_we_did_not_see_take_off_says_unknown_instead_of_guessing() -> None:
    views = [
        _view(1, state=PlayerState.TRAVELING, description="Traveling from South Africa to Torn",
              plane="light_aircraft", since=None)
    ]
    text = _text(target_board_embed(views, now=_NOW))
    assert "landing time unknown" in text
    assert "lands ~" not in text


def test_flight_past_its_estimate_reads_landing_now() -> None:
    views = [
        _view(1, state=PlayerState.TRAVELING, description="Traveling from Torn to Mexico",
              plane="light_aircraft", since=_NOW - timedelta(hours=2))
    ]
    assert "landing now" in _text(target_board_embed(views, now=_NOW))


def test_incoming_flights_sort_soonest_landing_first_and_unknown_last() -> None:
    def flight(i: int, minutes_ago: int | None) -> TargetView:
        return _view(
            i,
            state=PlayerState.TRAVELING,
            description="Traveling from Japan to Torn",
            plane="light_aircraft",
            since=None if minutes_ago is None else _NOW - timedelta(minutes=minutes_ago),
        )

    text = _text(target_board_embed([flight(1, None), flight(2, 5), flight(3, 100)], now=_NOW))
    assert text.index("Enemy3") < text.index("Enemy2") < text.index("Enemy1")


def test_abroad_enemy_sits_under_its_country_with_no_countdown() -> None:
    views = [_view(1, state=PlayerState.TRAVELING, description="In Argentina")]
    text = _text(target_board_embed(views, now=_NOW))
    assert "🇦🇷 __**Argentina**__ (1)" in text
    assert text.index("Argentina") < text.index("Enemy1")
    assert "<t:" not in text


def _abroad(i: int, country: str) -> TargetView:
    return _view(i, state=PlayerState.TRAVELING, description=f"In {country}")


def test_abroad_is_one_sub_list_per_country_a_to_z() -> None:
    views = [
        _abroad(1, "Japan"),
        _abroad(2, "Argentina"),
        _abroad(3, "UAE"),
        _abroad(4, "Japan"),
        _abroad(5, "Argentina"),
        _abroad(6, "United Kingdom"),
    ]
    lines = _text(target_board_embed(views, now=_NOW)).splitlines()
    assert lines[0] == "🌍 **ABROAD** (6)"
    headers = [line for line in lines if "__**" in line]
    assert headers == [
        "🇦🇷 __**Argentina**__ (2)",
        "🇯🇵 __**Japan**__ (2)",
        "🇦🇪 __**UAE**__ (1)",
        "🇬🇧 __**United Kingdom**__ (1)",
    ]
    # each enemy sits under its own country, best target first
    body = "\n".join(lines)
    assert body.index("Argentina") < body.index("Enemy2") < body.index("Enemy5")
    assert body.index("Japan") < body.index("Enemy1") < body.index("Enemy4") < body.index("UAE")
    assert body.count("**ABROAD**") == 1  # one ABROAD header, not one per country


def test_unknown_destination_still_gets_a_header() -> None:
    text = _text(target_board_embed([_abroad(1, "Atlantis")], now=_NOW))
    assert "🌍 __**Atlantis**__ (1)" in text


def test_abroad_countries_lists_each_country_once_a_to_z() -> None:
    views = [_abroad(1, "Japan"), _abroad(2, "Canada"), _abroad(3, "Japan"), _view(4)]
    assert abroad_countries(views, now=_NOW) == [("Canada", 1), ("Japan", 2)]
    assert abroad_countries([_view(1)], now=_NOW) == []


def test_country_embed_shows_only_that_country() -> None:
    views = [_abroad(1, "Japan"), _abroad(2, "Canada"), _abroad(3, "Japan"), _view(4)]
    embeds = country_embed(views, "japan", now=_NOW)
    text = _text(embeds)
    assert "Enemy1" in text and "Enemy3" in text
    assert "Enemy2" not in text and "Enemy4" not in text
    assert "JAPAN" in (embeds[0].title or "")
    assert "user2ID=1001" in text  # still click-to-attack


def test_country_embed_when_everyone_has_left() -> None:
    text = _text(country_embed([_view(1)], "Japan", now=_NOW))
    assert "No enemies are in or flying to Japan right now" in text


def test_abroad_enemy_name_is_a_clickable_attack_link() -> None:
    views = [_view(1, state=PlayerState.TRAVELING, description="In Argentina")]
    text = _text(target_board_embed(views, now=_NOW))
    assert "[Enemy1](https://www.torn.com/page.php?sid=attack&user2ID=1001)" in text


def test_flying_players_are_not_clickable() -> None:
    """Someone in the air cannot be attacked, so their name is plain text."""
    views = [
        _view(1, state=PlayerState.TRAVELING, description="Traveling from Torn to Japan",
              plane="light_aircraft", since=_NOW),
        _view(2, state=PlayerState.TRAVELING, description="Traveling from Japan to Torn",
              plane="light_aircraft", since=_NOW),
    ]
    assert "user2ID" not in _text(target_board_embed(views, now=_NOW))


# --- size limits --------------------------------------------------------------


def test_a_49_player_roster_shows_every_enemy_within_discords_limits() -> None:
    def name(i: int) -> str:
        return f"Plr{i:03d}x"  # unique, so "Plr001x" can never match inside another name

    views = [_view(i, name=name(i)) for i in range(1, 17)]
    views += [
        _view(i, name=name(i), state=PlayerState.HOSPITAL, description="In hospital",
              until=_NOW + timedelta(minutes=i))
        for i in range(17, 30)
    ]
    views += [
        _view(i, name=name(i), state=PlayerState.TRAVELING, description="In Japan")
        for i in range(30, 43)
    ]
    views += [
        _view(i, name=name(i), state=PlayerState.TRAVELING,
              description="Traveling from Torn to Japan", plane="light_aircraft", since=_NOW)
        for i in range(43, 50)
    ]
    embeds = target_board_embed(views, now=_NOW)
    text = _text(embeds)
    assert len(embeds) == 1  # one box, so it reads as one list (regression: it was 2 boxes)
    for i in range(1, 50):
        assert name(i) in text
    assert "more not shown" not in text
    assert "(cont.)" not in text
    assert len(embeds[0].description or "") <= 4096
    assert all(len(f.value or "") <= 1024 for f in embeds[0].fields)
    assert _message_size(embeds) <= 6000


def test_an_oversized_roster_never_produces_a_message_discord_would_reject() -> None:
    """Even two messages can't hold 200 enemies: still valid, and it says so."""
    embeds = target_board_embed([_view(i) for i in range(1, 201)], now=_NOW)
    assert len(embeds) == 2  # one embed per message, never more than two messages
    for embed in embeds:
        assert len(embed.description or "") <= 4096
        assert len(embed.fields) <= 25
        assert all(len(f.value or "") <= 1024 for f in embed.fields)
        assert _message_size([embed]) <= 6000
    assert "more not shown" in _text(embeds)  # and it says so, instead of silently dropping


def _names(embed: discord.Embed, prefix: str) -> list[str]:
    return [
        word.strip("[]*")
        for line in _text([embed]).splitlines()
        for word in line.replace("](", " ").split()
        if word.strip("[]*").startswith(prefix)
    ]


def _war_sized_roster() -> list[TargetView]:
    """87 enemies, like the live real war: too much for one message at full detail."""
    def name(i: int) -> str:
        return f"Foe{i:03d}xx"

    views = [_view(i, name=name(i)) for i in range(1, 41)]
    views += [
        _view(i, name=name(i), state=PlayerState.HOSPITAL, description="In hospital",
              until=_NOW + timedelta(minutes=i))
        for i in range(41, 71)
    ]
    views += [
        _view(i, name=name(i), state=PlayerState.TRAVELING, description="In Japan")
        for i in range(71, 88)
    ]
    return views


def test_a_roster_too_big_for_one_message_splits_into_two_equal_halves() -> None:
    embeds = target_board_embed(_war_sized_roster(), now=_NOW)
    assert len(embeds) == 2
    first, second = (_names(e, "Foe") for e in embeds)
    # every enemy shown exactly once, in board order, nothing cut
    assert first + second == [f"Foe{i:03d}xx" for i in range(1, 88)]
    assert abs(len(first) - len(second)) <= 1
    assert "more not shown" not in _text(embeds)
    for embed in embeds:
        assert _message_size([embed]) <= 6000
        assert len(embed.description or "") <= 4096
        assert all(len(f.value or "") <= 1024 for f in embed.fields)


def test_split_board_keeps_full_detail_on_every_line() -> None:
    embeds = target_board_embed(_war_sized_roster(), now=_NOW)
    rows = [line for line in _text(embeds).splitlines() if "Foe" in line]
    assert len(rows) == 87
    assert all("FF 1.50" in row for row in rows)  # not the shortened lines
    hospital = [row for row in rows if "out <t:" in row]
    assert hospital and all(":t>)" in row for row in hospital)  # clock time kept too


def test_split_board_titles_the_parts_and_marks_a_continued_section() -> None:
    embeds = target_board_embed(_war_sized_roster(), now=_NOW)
    assert [e.title for e in embeds] == ["⚔️ TARGET BOARD (1/2)", "⚔️ TARGET BOARD (2/2)"]
    # 87 split 44/43: the cut lands inside HOSPITAL, so part 2 opens by repeating it
    second = _text([embeds[1]])
    assert second.startswith("🔴 **HOSPITAL / JAIL — out soonest first** (30) — continued")
    assert _text([embeds[0]]).count("HOSPITAL") == 1


def test_a_roster_that_fits_stays_one_message_with_the_plain_title() -> None:
    embeds = target_board_embed([_view(i) for i in range(1, 11)], now=_NOW)
    assert len(embeds) == 1
    assert embeds[0].title == "⚔️ TARGET BOARD"


def test_a_list_longer_than_one_description_continues_inside_the_same_box() -> None:
    """44 available targets (~4.6k characters) do not fit in one description (4096), so the
    rest continues in a field of the SAME embed — not in a second box."""

    def name(i: int) -> str:
        return f"Plr{i:03d}xxxxxxxxxxxx"

    views = [_view(i, name=name(i)) for i in range(1, 45)]
    embeds = target_board_embed(views, now=_NOW)
    assert len(embeds) == 1
    assert embeds[0].fields  # the overflow lives in fields
    text = _text(embeds)
    for i in range(1, 45):
        assert name(i) in text
    assert text.count("AVAILABLE") == 1  # one section, not a repeated header
    assert "(cont.)" not in text
    assert "more not shown" not in text


# --- online highlight --------------------------------------------------------


def test_online_player_is_bold_with_a_marker_and_the_others_are_not() -> None:
    views = [
        _view(1, online="Online"),
        _view(2, online="Idle"),
        _view(3, online="Offline"),
        _view(4, online=None),  # not known yet: never shown as online
    ]
    lines = _text(target_board_embed(views, now=_NOW)).splitlines()

    def line(n: int) -> str:
        return next(x for x in lines if f"user2ID=100{n}" in x)

    assert "⚡" in line(1) and "**[Enemy1]" in line(1)
    for n in (2, 3, 4):
        assert "⚡" not in line(n)
        assert "**" not in line(n)


def test_online_highlight_works_on_every_kind_of_line() -> None:
    views = [
        _view(1, state=PlayerState.HOSPITAL, description="In hospital",
              until=_NOW + timedelta(hours=1), online="Online"),
        _view(2, state=PlayerState.TRAVELING, description="In Japan", online="Online"),
        _view(3, state=PlayerState.TRAVELING, description="Traveling from Torn to Japan",
              plane="airliner", since=_NOW, online="Online"),
    ]
    text = _text(target_board_embed(views, now=_NOW))
    assert "**Enemy1**" in text  # not clickable, still bold
    assert "**[Enemy2](" in text  # abroad is clickable and bold
    assert "**Enemy3**" in text
    assert text.count("⚡") == 3  # one per online player (the footer legend is not in this text)


def test_online_name_with_markdown_characters_cannot_break_the_bold() -> None:
    text = _text(target_board_embed([_view(1, name="a*b_c", online="Online")], now=_NOW))
    assert r"a\*b\_c" in text


def test_footer_explains_the_online_marker() -> None:
    embed = target_board_embed([_view(1)], now=_NOW)[0]
    assert "⚡ = online now" in embed.footer.text


def test_online_marker_survives_the_shortened_lines_used_for_big_rosters() -> None:
    views = [_view(i, online="Online") for i in range(1, 121)]
    text = _text(target_board_embed(views, now=_NOW))
    assert "⚡" in text and "**" in text


def test_online_first_then_idle_then_offline_in_available_and_each_country() -> None:
    views = [
        _view(1, online="Offline"),
        _view(2, online="Idle"),
        _view(3, online="Online"),
        _view(4, online=None),
        _view(5, online="Online"),
        _view(6, state=PlayerState.TRAVELING, description="In Japan", online="Offline"),
        _view(7, state=PlayerState.TRAVELING, description="In Japan", online="Online"),
        _view(8, state=PlayerState.TRAVELING, description="In China", online="Online"),
    ]
    text = _text(target_board_embed(views, now=_NOW))
    # AVAILABLE: online (best ranked first), idle, then offline / unknown by rank
    order = [text.index(f"Enemy{i}]") for i in (3, 5, 2, 1, 4)]
    assert order == sorted(order)
    # ABROAD: countries stay A to Z; online first inside each country
    assert text.index("China") < text.index("Enemy8]") < text.index("Japan")
    assert text.index("Japan") < text.index("Enemy7]") < text.index("Enemy6]")


def test_online_does_not_reorder_hospital() -> None:
    views = [
        _view(1, state=PlayerState.HOSPITAL, description="In hospital",
              until=_NOW + timedelta(minutes=5), online="Offline"),
        _view(2, state=PlayerState.HOSPITAL, description="In hospital",
              until=_NOW + timedelta(minutes=50), online="Online"),
    ]
    text = _text(target_board_embed(views, now=_NOW))
    assert text.index("Enemy1") < text.index("Enemy2")  # still out-soonest first


def _flying_to(i: int, country: str, minutes_ago: int = 5) -> TargetView:
    return _view(i, state=PlayerState.TRAVELING, description=f"Traveling from Torn to {country}",
                 plane="light_aircraft", since=_NOW - timedelta(minutes=minutes_ago))


def test_enemies_flying_out_are_listed_under_their_destination_below_those_there() -> None:
    views = [_flying_to(1, "Japan"), _abroad(2, "Japan"), _abroad(3, "Canada"), _abroad(4, "Japan")]
    text = _text(target_board_embed(views, now=_NOW))
    assert "🌍 **ABROAD** (4)" in text
    assert "🇯🇵 __**Japan**__ (2 + 1 flying in)" in text
    assert "🇨🇦 __**Canada**__ (1)" in text
    # Canada, then Japan: those already in Japan first, the flyer last
    assert (
        text.index("Canada") < text.index("Enemy3") < text.index("Japan")
        < text.index("Enemy2") < text.index("Enemy4") < text.index("Enemy1")
    )
    assert "user2ID=1001" not in text  # still in the air: not attackable yet


def test_several_flyers_to_one_country_land_soonest_first() -> None:
    views = [_flying_to(1, "UAE", minutes_ago=5), _flying_to(2, "UAE", minutes_ago=60)]
    text = _text(target_board_embed(views, now=_NOW))
    assert text.index("Enemy2") < text.index("Enemy1")


def test_country_picker_and_private_view_include_flyers() -> None:
    views = [_flying_to(1, "Japan"), _abroad(2, "Japan"), _flying_to(3, "Mexico"), _view(4)]
    assert abroad_countries(views, now=_NOW) == [("Japan", 2), ("Mexico", 1)]
    text = _text(country_embed(views, "Japan", now=_NOW))
    assert "Enemy1" in text and "Enemy2" in text
    assert "Enemy3" not in text and "Enemy4" not in text


def test_flights_home_stay_in_landing_in_torn() -> None:
    views = [_view(1, state=PlayerState.TRAVELING, description="Traveling from Japan to Torn",
                   plane="light_aircraft", since=_NOW - timedelta(minutes=5))]
    text = _text(target_board_embed(views, now=_NOW))
    assert "LANDING IN TORN" in text
    assert "ABROAD" not in text
