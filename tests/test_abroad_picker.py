from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import discord

from app.discord.abroad_picker import ABROAD_PICKER_ID, _picked_country, abroad_picker


def _interaction(kind: discord.InteractionType, data: dict[str, Any]) -> discord.Interaction:
    return cast(discord.Interaction, SimpleNamespace(type=kind, data=data))


async def test_picker_lists_each_country_with_its_flag_and_count() -> None:
    view = abroad_picker([("Argentina", 3), ("Japan", 1)])
    [select] = view.children
    assert isinstance(select, discord.ui.Select)
    assert select.custom_id == ABROAD_PICKER_ID
    assert [(o.label, o.value, str(o.emoji)) for o in select.options] == [
        ("Argentina (3)", "Argentina", "🇦🇷"),
        ("Japan (1)", "Japan", "🇯🇵"),
    ]
    assert view.timeout is None  # must not stop answering while the board stays up


async def test_no_countries_means_an_empty_view_that_clears_the_drop_down() -> None:
    assert abroad_picker([]).children == []


async def test_more_countries_than_discord_allows_are_capped() -> None:
    view = abroad_picker([(f"Place{i:02d}", 1) for i in range(30)])
    [select] = view.children
    assert isinstance(select, discord.ui.Select)
    assert len(select.options) == 25


def test_only_our_drop_down_is_picked_up() -> None:
    comp = discord.InteractionType.component
    assert (
        _picked_country(_interaction(comp, {"custom_id": ABROAD_PICKER_ID, "values": ["Japan"]}))
        == "Japan"
    )
    assert _picked_country(_interaction(comp, {"custom_id": "other", "values": ["Japan"]})) is None
    assert (
        _picked_country(_interaction(comp, {"custom_id": ABROAD_PICKER_ID, "values": []})) is None
    )
    assert (
        _picked_country(
            _interaction(
                discord.InteractionType.application_command, {"custom_id": ABROAD_PICKER_ID}
            )
        )
        is None
    )
