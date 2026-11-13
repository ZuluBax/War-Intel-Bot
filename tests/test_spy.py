from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.fernet import Fernet

from app.config.guild_config import GuildConfigService
from app.config.settings import Settings
from app.database.database import Database
from app.database.models import ConfirmedStat, Faction, StatEstimate, TargetRanking, War
from app.database.repositories import ConfirmedStatRepository, PlayerRepository
from app.discord.embeds import player_intel_embed, spy_report_text
from app.discord.matchup_commands import matchup_embed, torn_id_from_nickname
from app.discord.target_board import target_board_embed
from app.services.crypto import SecretCipher
from app.torn.models import PlayerState
from app.utils.time import utcnow
from app.war.intelligence import summarise
from app.war.matchups import build_matchups
from app.war.multi_guild import MultiGuildWarRunner
from app.war.roster import Roster, RosterEntry
from app.war.spy import SPY_TRUSTED_FOR, best_strength, parse_spy_report
from app.war.summary import load_target_view
from app.war.target_service import TargetingService, TargetView
from tests.fake_gateway import FakeGuildGateway

GUILD = 111
OTHER_GUILD = 222
WAR_ID = 1

# --- reading a pasted report ----------------------------------------------------


def test_reads_a_multi_line_torn_report() -> None:
    report = parse_spy_report(
        "Name: Druggo [2345678]\nLevel: 60\n"
        "Strength: 1,000,000,000\nDefense: 2,000,000,000\n"
        "Speed: 500,000,000\nDexterity: 500,000,000\nTotal: 4,000,000,000"
    )
    assert (report.strength, report.defense, report.speed, report.dexterity) == (
        1e9,
        2e9,
        5e8,
        5e8,
    )
    assert report.total == 4e9
    assert report.player_id == 2345678


def test_reads_a_report_folded_onto_one_line_with_short_units() -> None:
    # Discord folds pasted lines; TornStats-style exports use 4.5b / 300m.
    report = parse_spy_report("Str: 1.5b Def: 2b Spd: 600m Dex: 400m Total Battle Stats: 4.5b")
    assert report.strength == 1.5e9 and report.dexterity == 4e8
    assert report.total == 4.5e9


def test_unknown_stats_stay_empty_and_the_total_is_summed_only_when_all_four_known() -> None:
    partial = parse_spy_report("Strength: 1,000 Defense: N/A Speed: 3,000 Dexterity: 4,000")
    assert partial.defense is None and partial.best_total is None
    full = parse_spy_report("Strength: 1,000 Defense: 2,000 Speed: 3,000 Dexterity: 4,000")
    assert full.best_total == 10_000


def test_text_with_no_stats_is_empty() -> None:
    assert parse_spy_report("hello there").is_empty
    assert parse_spy_report("").is_empty
    # Other "total" lines are not mistaken for battle stats.
    assert parse_spy_report("Total networth: $5,000,000").is_empty


def test_profile_link_id_is_found() -> None:
    assert parse_spy_report("profiles.php?XID=42 Strength: 5").player_id == 42


# --- spy vs estimate ------------------------------------------------------------


def test_a_recent_spy_beats_the_estimate_and_an_old_one_does_not() -> None:
    now = utcnow()
    fresh = best_strength(estimate=1e9, spy_total=3e9, spy_reported_at=now, now=now)
    assert (fresh.value, fresh.confirmed) == (3e9, True)
    stale_at = now - SPY_TRUSTED_FOR - timedelta(minutes=1)
    stale = best_strength(estimate=1e9, spy_total=3e9, spy_reported_at=stale_at, now=now)
    assert (stale.value, stale.confirmed) == (1e9, False)
    assert best_strength(estimate=None, spy_total=None, spy_reported_at=None).value is None


# --- storage stays per server ---------------------------------------------------


async def _seed(database: Database) -> dict[int, int]:
    """A war in GUILD with two enemies, both ranked; returns torn id -> internal id."""
    async with database.session() as session:
        us = Faction(torn_faction_id=1, name="Us", is_ours=True)
        them = Faction(torn_faction_id=2, name="Them", is_ours=False)
        session.add_all([us, them])
        await session.flush()
        session.add(
            War(
                id=WAR_ID,
                guild_id=GUILD,
                torn_war_id=1,
                our_faction_id=us.id,
                enemy_faction_id=them.id,
                status="WAR_ACTIVE",
            )
        )
        ids = {}
        for torn_id, bs in ((501, 1e9), (502, 2e9)):
            p = await PlayerRepository(session).upsert(torn_id, name=f"E{torn_id}", level=50)
            await session.flush()
            session.add(StatEstimate(player_id=p.id, estimated_bs=bs, fair_fight=2.0))
            session.add(
                TargetRanking(war_id=WAR_ID, player_id=p.id, rank=1, priority="HIGH", score=1.0)
            )
            ids[torn_id] = p.id
        return ids


async def _spy(database: Database, player_id: int, total: float, *, guild: int = GUILD) -> None:
    async with database.session() as session:
        ConfirmedStatRepository(session).add(
            ConfirmedStat(player_id=player_id, guild_id=guild, total=total, reported_at=utcnow())
        )


async def test_a_report_is_only_seen_by_the_server_that_filed_it(database: Database) -> None:
    ids = await _seed(database)
    await _spy(database, ids[501], 9e9, guild=OTHER_GUILD)
    async with database.session() as session:
        repo = ConfirmedStatRepository(session)
        assert await repo.latest_for_player(GUILD, ids[501]) is None
        assert await repo.latest_for_players(GUILD, [ids[501]]) == {}
        other = await repo.latest_for_player(OTHER_GUILD, ids[501])
        assert other is not None and other.total == 9e9


async def test_the_newest_report_wins(database: Database) -> None:
    ids = await _seed(database)
    async with database.session() as session:
        repo = ConfirmedStatRepository(session)
        old = utcnow() - timedelta(days=1)
        repo.add(ConfirmedStat(player_id=ids[501], guild_id=GUILD, total=1.0, reported_at=old))
        repo.add(ConfirmedStat(player_id=ids[501], guild_id=GUILD, total=2.0, reported_at=utcnow()))
    async with database.session() as session:
        latest = await ConfirmedStatRepository(session).latest_for_players(GUILD, [ids[501]])
    assert latest[ids[501]].total == 2.0


# --- the spy number reaches the ranking and the board ----------------------------


def _roster() -> Roster:
    return Roster(
        war_id=WAR_ID,
        enemy_faction_id=2,
        entries=[
            RosterEntry(
                player_id=501, name="E501", level=50, estimated_bs=1e9, state=PlayerState.OKAY
            ),
            RosterEntry(
                player_id=502, name="E502", level=50, estimated_bs=2e9, state=PlayerState.OKAY
            ),
        ],
    )


async def test_ranking_uses_this_servers_spy_total(database: Database) -> None:
    ids = await _seed(database)
    # The "weak" one is really the strongest — a spy says so.
    await _spy(database, ids[501], 9e9)

    views = await TargetingService(database).refresh(WAR_ID, _roster())

    assert [v.player_id for v in views] == [502, 501]
    by_id = {v.player_id: v for v in views}
    assert (by_id[501].estimated_bs, by_id[501].confirmed) == (9e9, True)
    assert by_id[502].confirmed is False


async def test_ranking_ignores_another_servers_spy(database: Database) -> None:
    ids = await _seed(database)
    await _spy(database, ids[501], 9e9, guild=OTHER_GUILD)
    views = await TargetingService(database).refresh(WAR_ID, _roster())
    assert [v.player_id for v in views] == [501, 502]
    assert not any(v.confirmed for v in views)


async def test_targets_command_view_shows_the_spy_total(database: Database) -> None:
    ids = await _seed(database)
    await _spy(database, ids[502], 7e9)
    async with database.session() as session:
        war = await session.get(War, WAR_ID)
        assert war is not None
        views = {v.player_id: v for v in await load_target_view(session, war)}
    assert (views[502].estimated_bs, views[502].confirmed) == (7e9, True)
    assert (views[501].estimated_bs, views[501].confirmed) == (1e9, False)


def _view(
    pid: int, bs: float | None, *, confirmed: bool = False, available: bool = True
) -> TargetView:
    return TargetView(
        player_id=pid,
        name=f"E{pid}",
        level=50,
        rank=1,
        score=1.0,
        priority="HIGH",
        state=PlayerState.OKAY if available else PlayerState.HOSPITAL,
        is_available=available,
        estimated_bs=bs,
        confirmed=confirmed,
        status_description="Okay",
    )


def test_board_marks_a_spy_number() -> None:
    embed = target_board_embed([_view(1, 3e9, confirmed=True), _view(2, 1e9)])[0]
    text = embed.description or ""
    assert "3.00B🔍" in text
    assert "1.00B🔍" not in text
    assert embed.footer.text is not None and "spy report" in embed.footer.text


def test_player_card_shows_the_spy_report_apart_from_the_estimate() -> None:
    spy = ConfirmedStat(
        player_id=1,
        guild_id=GUILD,
        strength=1e9,
        defense=1e9,
        speed=1e9,
        dexterity=1e9,
        total=4e9,
        reported_by_discord_id=99,
        reported_at=utcnow(),
        note="from Bob",
    )
    embed = player_intel_embed(
        name="E1",
        player_id=1,
        level=50,
        state=PlayerState.OKAY,
        trend=summarise([]),
        fair_fight=None,
        spy=spy,
    )
    field = next(f for f in embed.fields if "Spy report" in (f.name or ""))
    assert "Total 4.00B" in (field.value or "")
    assert "<@99>" in (field.value or "") and "from Bob" in (field.value or "")


def test_an_old_report_says_it_is_no_longer_used() -> None:
    spy = ConfirmedStat(
        player_id=1, guild_id=GUILD, total=4e9, reported_at=datetime(2020, 1, 1, tzinfo=UTC)
    )
    assert "estimate is used" in spy_report_text(spy)


# --- matchups -------------------------------------------------------------------


def test_matchups_sort_enemies_into_good_even_and_avoid() -> None:
    m = build_matchups(
        2e9,
        [
            _view(1, 1e9),  # 50% -> good
            _view(2, 1.4e9),  # 70% -> good, the toughest good fight so listed first
            _view(3, 2e9),  # 100% -> even
            _view(4, 5e9),  # 250% -> avoid
            _view(5, None),  # no number
            _view(6, 0.5e9, available=False),  # good but in hospital -> after available ones
        ],
    )
    assert [x.target.player_id for x in m.good] == [2, 1, 6]
    assert [x.target.player_id for x in m.even] == [3]
    assert [x.target.player_id for x in m.avoid] == [4]
    assert m.unknown == 1


def test_matchups_need_a_positive_strength() -> None:
    with pytest.raises(ValueError):
        build_matchups(0, [])


def test_matchup_embed_fits_with_a_huge_roster() -> None:
    m = build_matchups(1e9, [_view(i, i * 1e7) for i in range(1, 300)])
    embed = matchup_embed(our_name="Me", our_strength=1e9, our_confirmed=False, matchups=m)
    assert len(embed) <= 6000 and len(embed.description or "") <= 4096
    assert "more" in (embed.description or "")


@pytest.mark.parametrize(
    ("nick", "torn_id"),
    [("Zulu [2345678]", 2345678), ("Zulu", None), (None, None), ("[1] x", 1)],
)
def test_torn_id_from_nickname(nick: str | None, torn_id: int | None) -> None:
    assert torn_id_from_nickname(nick) == torn_id


@respx.mock
async def test_our_estimate_is_fetched_once_then_remembered(
    database: Database, settings: Settings
) -> None:
    gc = GuildConfigService(database, SecretCipher(Fernet.generate_key()))
    await gc.set_faction(GUILD, 100, by=1)
    await gc.set_torn_key(GUILD, "torn-key", by=1)
    await gc.set_ffscouter_key(GUILD, "ffs-key", by=1)
    route = respx.get(f"{settings.ffscouter_api_base_url}/get-stats").mock(
        return_value=httpx.Response(200, json=[{"player_id": 7, "bs_estimate": 3_000_000_000}])
    )
    runner = MultiGuildWarRunner(
        database, gc, settings, make_gateway=lambda _gid: FakeGuildGateway()
    )
    assert await runner.strength_estimate(GUILD, 7) == 3e9
    assert await runner.strength_estimate(GUILD, 7) == 3e9
    assert route.call_count == 1
    await runner.aclose()


async def test_no_ffscouter_key_means_no_estimate(database: Database, settings: Settings) -> None:
    gc = GuildConfigService(database, SecretCipher(Fernet.generate_key()))
    await gc.set_faction(GUILD, 100, by=1)
    await gc.set_torn_key(GUILD, "torn-key", by=1)
    runner = MultiGuildWarRunner(
        database, gc, settings, make_gateway=lambda _gid: FakeGuildGateway()
    )
    assert await runner.strength_estimate(GUILD, 7) is None
    await runner.aclose()


async def test_spy_and_matchups_are_registered_and_config_is_gone(settings: Settings) -> None:
    from app.discord.bot import TornWarIntelBot
    from app.discord.commands import register_commands

    bot = TornWarIntelBot(settings)
    register_commands(bot)
    names = {c.name for c in bot.tree.get_commands()}
    assert {"spy", "matchups", "setup", "targets"} <= names
    assert "config" not in names
    spy = bot.tree.get_command("spy")
    assert spy is not None
    assert {c.name for c in spy.commands} == {"add", "show", "remove"}  # type: ignore[union-attr]
