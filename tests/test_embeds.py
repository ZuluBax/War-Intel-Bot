from __future__ import annotations

import discord

from app.discord.embeds import roster_embed
from app.war.roster import Roster, RosterEntry


def _roster(count: int) -> Roster:
    return Roster(
        war_id=1,
        enemy_faction_id=60001,
        enemy_faction_name="Rival Crew",
        entries=[
            RosterEntry(player_id=i, name=f"Enemy{i}", level=10 + i, estimated_bs=float(i))
            for i in range(count)
        ],
    )


# --- roster_embed -------------------------------------------------------


def test_roster_embed_fits_on_one_page_returns_one_embed() -> None:
    embeds = roster_embed(_roster(10))
    assert len(embeds) == 1
    assert "(1/" not in embeds[0].title  # no page indicator when there's only one page
    for entry in _roster(10).entries:
        assert entry.name in embeds[0].description


def _rows(embeds: list[discord.Embed]) -> int:
    """Table rows across all embeds (each row starts with the fixture's 'Enemy' name)."""
    return sum((e.description or "").count("\nEnemy") for e in embeds)


def _message_size(embeds: list[discord.Embed]) -> int:
    """Discord counts titles, descriptions, fields and footers of every embed in a message."""
    return sum(
        len(e.title or "")
        + len(e.description or "")
        + len(e.footer.text or "")
        + sum(len(f.name or "") + len(f.value or "") for f in e.fields)
        for e in embeds
    )


def test_roster_embed_shows_a_49_member_roster_as_one_list() -> None:
    # Regression test: a 49-member roster used to be split into two embeds at 35 rows,
    # although a row is ~38 characters and one embed holds about 100 of them.
    embeds = roster_embed(_roster(49))
    assert len(embeds) == 1
    assert _rows(embeds) == 49
    assert "(1/" not in embeds[0].title


def test_roster_embed_shows_a_full_100_member_faction_as_one_list() -> None:
    """100 is the most members a Torn faction can have."""
    embeds = roster_embed(_roster(100))
    assert len(embeds) == 1
    assert _rows(embeds) == 100
    assert len(embeds[0].description) <= 4096
    assert _message_size(embeds) <= 6000


def test_roster_embed_with_the_widest_possible_rows_still_fits_100_members() -> None:
    """Longest name, biggest stat and a Fair Fight value: the widest row there is."""
    roster = Roster(
        war_id=1,
        enemy_faction_id=1,
        entries=[
            RosterEntry(
                player_id=i,
                name="Enemy_LongestName1"[:16].ljust(16, "x"),
                level=100,
                estimated_bs=9.99e12,
                fair_fight=3.00,
            )
            for i in range(100)
        ],
    )
    embeds = roster_embed(roster)
    assert len(embeds) == 1
    assert len(embeds[0].description) <= 4096


def test_roster_embed_splits_an_oversized_list_instead_of_cutting_rows_off() -> None:
    embeds = roster_embed(_roster(250))
    assert len(embeds) > 1
    assert _rows(embeds) == 250  # every enemy still appears
    assert all(len(e.description or "") <= 4096 for e in embeds)
    assert all("PLAYER" in (e.description or "") for e in embeds)  # header on every page
    assert all(f"({i}/{len(embeds)})" in embeds[i - 1].title for i in range(1, len(embeds) + 1))


def test_roster_embed_empty_roster_still_returns_one_embed() -> None:
    embeds = roster_embed(_roster(0))
    assert len(embeds) == 1
    assert "0 enemies" in embeds[0].fields[0].value
