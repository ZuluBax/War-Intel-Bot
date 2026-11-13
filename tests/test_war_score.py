from __future__ import annotations

import time
from typing import cast

import pytest
from sqlalchemy import select

from app.database.database import Database
from app.database.models import DiscordChannel
from app.discord import war_channels
from app.discord.embeds import war_score_embed
from app.discord.gateway import GuildGateway
from app.discord.war_channels import (
    CHANNEL_LAYOUT,
    SCORE_PURPOSE,
    WarBoard,
    WarChannelManager,
    WarLogBoard,
)
from app.discord.war_publish import DiscordWarPublisher
from app.torn.models import RankedWar, RankedWarFaction
from app.utils.time import utcnow
from app.war.roster import Roster
from app.war.score import WarScore, score_from_war
from tests.fake_gateway import FakeGuildGateway
from tests.test_war_channels import _seed_war

OUR_ID = 10001
ENEMY_ID = 60001


def _score(**overrides: object) -> WarScore:
    values: dict[str, object] = {
        "our_name": "Ours & Co",
        "enemy_name": "Rival Crew",
        "our_score": 1200,
        "enemy_score": 900,
        "target_score": 5000,
    }
    values.update(overrides)
    return WarScore(**values)  # type: ignore[arg-type]


# --- score_from_war ----------------------------------------------------------


def _war(*, factions: tuple[RankedWarFaction, ...]) -> RankedWar:
    return RankedWar(war_id=1, start=utcnow(), end=None, target_score=5000, factions=factions)


def test_score_from_war_is_from_our_side_whichever_order_torn_lists_the_factions() -> None:
    war = _war(
        factions=(
            RankedWarFaction(ENEMY_ID, "Rival Crew", 900),
            RankedWarFaction(OUR_ID, "Ours & Co", 1200),
        )
    )
    score = score_from_war(war, OUR_ID, finished=False, result=None)
    assert score is not None
    assert (score.our_name, score.our_score) == ("Ours & Co", 1200)
    assert (score.enemy_name, score.enemy_score) == ("Rival Crew", 900)
    assert score.target_score == 5000
    assert score.lead == 300


def test_score_from_war_is_none_when_a_faction_is_missing() -> None:
    war = _war(factions=(RankedWarFaction(OUR_ID, "Ours & Co", 1200),))
    assert score_from_war(war, OUR_ID, finished=False, result=None) is None


def test_lead_is_unknown_when_torn_omits_a_score() -> None:
    assert _score(enemy_score=None).lead is None


# --- war_score_embed ---------------------------------------------------------


def _text(embed: object) -> str:
    import discord

    assert isinstance(embed, discord.Embed)
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name} {f.value}" for f in embed.fields]
    return "\n".join(parts)


def test_score_box_shows_both_scores_the_target_and_the_lead() -> None:
    text = _text(war_score_embed(_score()))
    assert "LIVE SCORE" in text
    assert "Ours & Co **1,200**" in text
    assert "Rival Crew **900**" in text
    assert "Target 5,000" in text
    assert "Leading by **300**" in text


def test_score_box_says_trailing_when_behind_and_tied_when_level() -> None:
    assert "Trailing by **50**" in _text(war_score_embed(_score(our_score=850)))
    assert "Tied" in _text(war_score_embed(_score(our_score=900)))


def test_score_box_shows_a_dash_not_a_fake_zero_for_a_missing_score() -> None:
    text = _text(war_score_embed(_score(our_score=None)))
    assert "Ours & Co **—**" in text
    assert "unavailable" in text


def test_score_box_colour_follows_who_is_ahead() -> None:
    ahead = war_score_embed(_score()).colour
    behind = war_score_embed(_score(our_score=100)).colour
    level = war_score_embed(_score(our_score=900)).colour
    assert len({ahead, behind, level}) == 3


def test_final_score_box_names_the_result() -> None:
    text = _text(war_score_embed(_score(finished=True, result="WIN")))
    assert "FINAL SCORE" in text
    assert "War won by **300** points" in text


def test_score_box_survives_missing_names_and_target() -> None:
    text = _text(war_score_embed(_score(our_name=None, enemy_name=None, target_score=None)))
    assert "Us **1,200**" in text
    assert "Enemy **900**" in text
    assert "Target" not in text


# --- DiscordWarPublisher.publish_score ---------------------------------------


def _gw(fake: FakeGuildGateway) -> GuildGateway:
    return cast(GuildGateway, fake)


def _publisher(database: Database, fake: FakeGuildGateway) -> DiscordWarPublisher:
    return DiscordWarPublisher(
        database,
        WarChannelManager(database, _gw(fake)),
        WarBoard(database, _gw(fake)),
        WarLogBoard(database, _gw(fake)),
    )


async def _row(database: Database, war_id: int, purpose: str) -> DiscordChannel | None:
    async with database.session() as session:
        return await session.scalar(
            select(DiscordChannel).where(
                DiscordChannel.war_id == war_id, DiscordChannel.purpose == purpose
            )
        )


async def test_score_box_is_its_own_message_in_the_information_channel(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = _publisher(database, fake)
    await publisher.publish_war(war_id, Roster(war_id=war_id, enemy_faction_id=ENEMY_ID))

    assert await publisher.publish_score(war_id, _score()) is True

    info = await _row(database, war_id, "information")
    assert info is not None
    # The info box plus the small score box, in the same channel — no extra channel.
    assert fake.message_count(info.channel_id) == 2
    assert len(fake.channels) == len(CHANNEL_LAYOUT)


async def test_score_box_is_edited_in_place_not_reposted(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = _publisher(database, fake)
    await publisher.publish_war(war_id, Roster(war_id=war_id, enemy_faction_id=ENEMY_ID))

    await publisher.publish_score(war_id, _score(our_score=1200))
    await publisher.publish_score(war_id, _score(our_score=1350))

    info = await _row(database, war_id, "information")
    score_row = await _row(database, war_id, SCORE_PURPOSE)
    assert info is not None and score_row is not None
    assert fake.message_count(info.channel_id) == 2  # still just info + score
    assert score_row.live_message_id is not None
    box = fake.messages[(info.channel_id, score_row.live_message_id)][0]
    assert "1,350" in _text(box)


async def test_score_box_reappears_if_someone_deletes_it(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = _publisher(database, fake)
    await publisher.publish_war(war_id, Roster(war_id=war_id, enemy_faction_id=ENEMY_ID))
    await publisher.publish_score(war_id, _score())

    info = await _row(database, war_id, "information")
    score_row = await _row(database, war_id, SCORE_PURPOSE)
    assert info is not None and score_row is not None and score_row.live_message_id is not None
    fake.delete_message(info.channel_id, score_row.live_message_id)

    # An unchanged box isn't re-sent every tick, so the repost comes on the periodic recheck.
    later = time.monotonic() + 301
    monkeypatch.setattr(war_channels.time, "monotonic", lambda: later)
    assert await publisher.publish_score(war_id, _score()) is True
    new_row = await _row(database, war_id, SCORE_PURPOSE)
    assert new_row is not None and new_row.live_message_id != score_row.live_message_id


async def test_score_box_waits_when_the_workspace_does_not_exist_yet(
    database: Database,
) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    # No publish_war() first, so there is no information channel to post into.
    assert await _publisher(database, fake).publish_score(war_id, _score()) is False
    assert fake.channels == {}


async def test_score_box_reports_false_while_discord_is_not_ready(database: Database) -> None:
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = _publisher(database, fake)
    await publisher.publish_war(war_id, Roster(war_id=war_id, enemy_faction_id=ENEMY_ID))

    fake._ready = False  # the fake has no public switch for going offline mid-test
    assert await publisher.publish_score(war_id, _score()) is False


async def test_score_box_does_not_make_war_information_look_shared(database: Database) -> None:
    """Regression (1 Oct): the score box shares #war-information on purpose, but it was
    counted as a second owner, so every refresh created yet another #war-information."""
    war_id = await _seed_war(database)
    fake = FakeGuildGateway()
    publisher = _publisher(database, fake)
    await publisher.publish_war(war_id, Roster(war_id=war_id, enemy_faction_id=ENEMY_ID))
    await publisher.publish_score(war_id, _score())
    channels_before = set(fake.channels)

    for _ in range(3):
        await publisher.publish_war(war_id, Roster(war_id=war_id, enemy_faction_id=ENEMY_ID))
        await publisher.publish_score(war_id, _score())

    assert set(fake.channels) == channels_before  # no new channels
    info = await _row(database, war_id, "information")
    score_row = await _row(database, war_id, SCORE_PURPOSE)
    assert info is not None and score_row is not None
    assert score_row.channel_id == info.channel_id
    assert fake.message_count(info.channel_id) == 2  # war info + score, once each
