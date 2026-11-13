from __future__ import annotations

import pytest

from app.discord.setup_commands import BOARD_LABELS, board_for_channel_name
from app.discord.war_channels import CHANNEL_LAYOUT


@pytest.mark.parametrize(
    ("name", "purpose"),
    [
        ("war-information", "information"),
        ("enemy-stats", "stats"),
        ("target-board", "targets"),
        ("war-log", "log"),
        ("enemy-activity", "activity"),
        ("📝-war-log", "log"),  # the bot's own emoji-prefixed names work too
        ("nv-target-board", "targets"),
    ],
)
def test_board_is_guessed_from_channel_name(name: str, purpose: str) -> None:
    assert board_for_channel_name(name) == purpose


@pytest.mark.parametrize("name", ["general", "war-logs", "stats", "my-war-information-old"])
def test_unrecognised_names_are_not_guessed(name: str) -> None:
    assert board_for_channel_name(name) is None


def test_every_board_has_a_label() -> None:
    assert set(BOARD_LABELS) == {p for p, _, _ in CHANNEL_LAYOUT}
