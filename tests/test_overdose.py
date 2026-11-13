from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import select

from app.database.database import Database
from app.database.models import Overdose, WarLogEvent
from app.database.repositories import OverdoseRepository, OverdoseView
from app.discord.target_board import target_board_embed
from app.ffscouter.client import FFScouterClient
from app.torn.faction import FactionService, _parse_status
from app.torn.models import FactionMember, FactionSummary, PlayerState, TornStatus
from app.utils.time import utcnow
from app.war.overdose import overdose_drug
from app.war.roster import EnemyRosterService
from app.war.target_service import TargetView

# --- parsing the hospital reason -----------------------------------------------


@pytest.mark.parametrize(
    ("details", "drug"),
    [
        ("Overdosed on Xanax", "Xanax"),  # the wording the owner sees on Torn
        ("overdosed on some Xanax", "Xanax"),
        ("Overdosed on <a href='/item.php'>Ecstasy</a>", "Ecstasy"),
        ("Hospitalized", None),
        ("Hospitalized by <a href='profiles.php?XID=1'>Someone</a>", None),
        (None, None),
        ("", None),
    ],
)
def test_overdose_drug(details: str | None, drug: str | None) -> None:
    assert overdose_drug(details) == drug


def test_torn_status_details_are_kept() -> None:
    status = _parse_status(
        {
            "description": "In hospital for 82 hrs",
            "details": "Overdosed on Xanax",
            "state": "Hospital",
            "until": 1789061000,
        }
    )
    assert status.details == "Overdosed on Xanax"
    assert _parse_status({"state": "Okay", "details": None}).details is None


# --- spotting it during roster refreshes ----------------------------------------


class _StubFaction:
    def __init__(self, members: list[FactionMember]) -> None:
        self.members = members

    async def get_members(self, faction_id: int) -> FactionSummary:
        return FactionSummary(faction_id=faction_id, members=tuple(self.members))


def _okay(pid: int) -> FactionMember:
    return FactionMember(player_id=pid, name=f"Enemy{pid}", status=TornStatus(PlayerState.OKAY))


def _overdosed(pid: int, *, hours_left: float = 80, drug: str = "Xanax") -> FactionMember:
    return FactionMember(
        player_id=pid,
        name=f"Enemy{pid}",
        status=TornStatus(
            PlayerState.HOSPITAL,
            description="In hospital for 80 hrs",
            details=f"Overdosed on {drug}",
            until=utcnow() + timedelta(hours=hours_left),
        ),
    )


def _service(database: Database, faction: _StubFaction) -> EnemyRosterService:
    return EnemyRosterService(database, cast(FactionService, faction), cast(FFScouterClient, None))


async def _rows(database: Database) -> list[Overdose]:
    async with database.session() as session:
        return list((await session.scalars(select(Overdose).order_by(Overdose.id))).all())


async def _log_messages(database: Database) -> list[str]:
    async with database.session() as session:
        rows = await session.scalars(select(WarLogEvent).where(WarLogEvent.kind == "OVERDOSE"))
        return [r.message for r in rows]


async def test_an_overdose_seen_happening_is_recorded_once_and_logged(database: Database) -> None:
    faction = _StubFaction([_okay(501)])
    svc = _service(database, faction)
    await svc.build(1, 777, fetch_estimates=False)  # announcement: first look, all fine

    faction.members = [_overdosed(501)]
    for _ in range(3):  # stays in hospital over several refreshes
        await svc.build(1, 777, fetch_estimates=False)

    rows = await _rows(database)
    assert len(rows) == 1
    assert (rows[0].drug, rows[0].before_watch) == ("Xanax", False)
    messages = await _log_messages(database)
    assert len(messages) == 1
    assert messages[0].startswith("Enemy501 overdosed on Xanax — in hospital until <t:")


async def test_an_overdose_from_before_the_first_look_is_kept_off_board_and_log(
    database: Database,
) -> None:
    svc = _service(database, _StubFaction([_overdosed(502)]))
    await svc.build(1, 777, fetch_estimates=False)
    await svc.build(1, 777, fetch_estimates=False)

    rows = await _rows(database)
    assert len(rows) == 1 and rows[0].before_watch is True
    assert await _log_messages(database) == []
    async with database.session() as session:
        assert await OverdoseRepository(session).since_watch(1) == []


async def test_a_bot_restart_mid_stay_does_not_record_it_again(database: Database) -> None:
    faction = _StubFaction([_okay(503)])
    await _service(database, faction).build(1, 777, fetch_estimates=False)
    faction.members = [_overdosed(503)]
    await _service(database, faction).build(1, 777, fetch_estimates=False)

    await _service(database, faction).build(1, 777, fetch_estimates=False)  # fresh process

    assert len(await _rows(database)) == 1


async def test_meds_shortening_the_stay_updates_the_release_time(database: Database) -> None:
    faction = _StubFaction([_okay(504)])
    svc = _service(database, faction)
    await svc.build(1, 777, fetch_estimates=False)
    faction.members = [_overdosed(504, hours_left=80)]
    await svc.build(1, 777, fetch_estimates=False)
    faction.members = [_overdosed(504, hours_left=10)]
    await svc.build(1, 777, fetch_estimates=False)

    rows = await _rows(database)
    assert len(rows) == 1
    assert rows[0].hospital_until is not None
    assert rows[0].hospital_until - utcnow() < timedelta(hours=11)


async def test_a_second_overdose_after_release_is_a_new_row(database: Database) -> None:
    faction = _StubFaction([_okay(505)])
    svc = _service(database, faction)
    await svc.build(1, 777, fetch_estimates=False)
    faction.members = [_overdosed(505, hours_left=0.001)]  # release time about to pass
    await svc.build(1, 777, fetch_estimates=False)
    async with database.session() as session:
        row = await session.scalar(select(Overdose))
        assert row is not None
        row.hospital_until = utcnow() - timedelta(minutes=1)  # released
    faction.members = [_okay(505)]
    await svc.build(1, 777, fetch_estimates=False)

    faction.members = [_overdosed(505)]
    await svc.build(1, 777, fetch_estimates=False)

    assert len(await _rows(database)) == 2
    assert len(await _log_messages(database)) == 2


async def test_an_ordinary_hospital_stay_is_not_an_overdose(database: Database) -> None:
    faction = _StubFaction([_okay(506)])
    svc = _service(database, faction)
    await svc.build(1, 777, fetch_estimates=False)
    faction.members = [
        FactionMember(
            player_id=506,
            status=TornStatus(
                PlayerState.HOSPITAL, details="Hospitalized", until=utcnow() + timedelta(hours=1)
            ),
        )
    ]
    await svc.build(1, 777, fetch_estimates=False)
    assert await _rows(database) == []


# --- the target-board section ----------------------------------------------------


def _target(pid: int) -> TargetView:
    return TargetView(
        rank=pid,
        player_id=pid,
        name=f"Enemy{pid}",
        level=50,
        score=1.0,
        priority="HIGH",
        state=PlayerState.OKAY,
        is_available=True,
        status_description="Okay",
    )


def _od(name: str, *, hours_left: float | None = 70) -> OverdoseView:
    now = utcnow()
    return OverdoseView(
        torn_player_id=9,
        name=name,
        drug="Xanax",
        first_seen_at=now,
        hospital_until=None if hours_left is None else now + timedelta(hours=hours_left),
    )


def _text(embeds: list) -> str:  # type: ignore[type-arg]
    e = embeds[0]
    return "\n".join([e.description or "", *(f.value or "" for f in e.fields)])


def test_board_lists_overdoses_at_the_bottom() -> None:
    text = _text(
        target_board_embed([_target(1)], overdoses=[_od("Druggo"), _od("Gone", hours_left=-1)])
    )
    assert "OVERDOSED SINCE THE WAR WAS ANNOUNCED** (2)" in text
    assert "🤢 Druggo — Xanax · out <t:" in text
    assert "🤢 Gone — Xanax · out of hospital" in text
    assert text.index("Enemy1") < text.index("OVERDOSED")


def test_board_has_no_overdose_section_when_there_are_none() -> None:
    assert "OVERDOSED" not in _text(target_board_embed([_target(1)]))


def test_overdose_section_fits_inside_discords_limit_with_a_huge_roster() -> None:
    embeds = target_board_embed(
        [_target(i) for i in range(1, 200)], overdoses=[_od(f"OD{i}") for i in range(15)]
    )
    assert all(len(e) <= 6000 for e in embeds)  # each is its own message
    # the overdose list is never the part that gets cut; it ends the last message
    assert "OD14" in _text(embeds[-1:])


async def test_an_overdose_stays_listed_for_the_rest_of_the_war(database: Database) -> None:
    # Owner, 9 Oct 2026: once found, keep it visible for the whole war, not just the stay.
    faction = _StubFaction([_okay(507)])
    svc = _service(database, faction)
    await svc.build(1, 777, fetch_estimates=False)
    faction.members = [_overdosed(507)]
    await svc.build(1, 777, fetch_estimates=False)
    faction.members = [_okay(507)]  # out of hospital
    await svc.build(1, 777, fetch_estimates=False)
    faction.members = []  # even after leaving the enemy faction
    await svc.build(1, 777, fetch_estimates=False)

    async with database.session() as session:
        listed = await OverdoseRepository(session).since_watch(1)
    assert [od.name for od in listed] == ["Enemy507"]


def test_a_long_overdose_list_stays_within_one_embed() -> None:
    from app.discord.summary_embeds import overdose_summary_embed

    embed = overdose_summary_embed([_od(f"Player{i:03}") for i in range(200)])
    assert embed is not None
    assert len(embed.description or "") <= 4096
    assert (embed.description or "").endswith("more")
    assert overdose_summary_embed([]) is None


def test_split_board_lists_overdoses_once_at_the_bottom_of_the_last_message() -> None:
    embeds = target_board_embed(
        [_target(i) for i in range(1, 90)], overdoses=[_od("Druggo")]
    )
    assert len(embeds) == 2
    assert "OVERDOSED" not in _text(embeds[:1])
    assert "🤢 Druggo" in _text(embeds[1:])
    assert all(len(e) <= 6000 for e in embeds)
