from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest

from app.torn.attacks import AttackService
from app.torn.client import TornClient
from app.torn.errors import TornAPIAccessLevelError
from tests.conftest import FakeTornClient
from tests.torn_fixtures import attack, attacks_page

OUR_ID = 10001
SINCE = datetime(2026, 9, 19, 18, 0, 0, tzinfo=UTC)
T0 = int(SINCE.timestamp())


def _service(fake: FakeTornClient) -> AttackService:
    return AttackService(cast(TornClient, fake))


def _query(path: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}


async def test_parses_a_real_shaped_attack() -> None:
    fake = FakeTornClient({r"/attacks": attacks_page(attack(1, T0 + 10))})
    [hit] = await _service(fake).list_attacks(OUR_ID, since=SINCE)

    assert hit.attack_id == 1
    assert hit.started == datetime.fromtimestamp(T0 + 10, tz=UTC)
    assert (hit.attacker_id, hit.attacker_name, hit.attacker_faction_id) == (
        1000021,
        "Our_Hitter",
        10001,
    )
    assert (hit.defender_name, hit.defender_faction_id, hit.defender_faction_name) == (
        "TheirTarget",
        60001,
        "Rival Crew",
    )
    assert hit.result == "Attacked"
    assert hit.respect_gain == pytest.approx(4.68)
    assert hit.is_ranked_war is True


async def test_stealthed_hit_on_us_has_no_attacker() -> None:
    row = attack(2, T0 + 20, attacker=None, defender=(1, "Ours", OUR_ID, "Ours & Co"))
    fake = FakeTornClient({r"/attacks": attacks_page(row)})
    [hit] = await _service(fake).list_attacks(OUR_ID, since=SINCE)

    assert hit.attacker_id is None
    assert hit.attacker_faction_id is None
    assert hit.defender_faction_id == OUR_ID


async def test_outside_hit_is_not_flagged_as_ranked_war() -> None:
    fake = FakeTornClient({r"/attacks": attacks_page(attack(3, T0 + 5, is_ranked_war=False))})
    [hit] = await _service(fake).list_attacks(OUR_ID, since=SINCE)
    assert hit.is_ranked_war is False


async def test_asks_torn_oldest_first_from_the_cursor_and_bounded_by_until() -> None:
    fake = FakeTornClient({r"/attacks": attacks_page()})
    until = datetime(2026, 9, 19, 20, 0, 0, tzinfo=UTC)
    await _service(fake).list_attacks(OUR_ID, since=SINCE, until=until)

    [path] = fake.calls
    assert urlparse(path).path == f"/faction/{OUR_ID}/attacks"
    assert _query(path) == {
        "limit": "100",
        "sort": "asc",
        "from": str(T0),
        "to": str(int(until.timestamp())),
    }


async def test_rows_missing_an_id_or_time_are_skipped_not_fatal() -> None:
    bad_id = attack(0, T0)
    bad_id["id"] = None
    bad_time = attack(9, T0)
    bad_time["started"] = "soon"
    fake = FakeTornClient(
        {r"/attacks": attacks_page(bad_id, bad_time, cast(Any, "junk"), attack(4, T0 + 1))}
    )
    hits = await _service(fake).list_attacks(OUR_ID, since=SINCE)
    assert [h.attack_id for h in hits] == [4]


async def test_pages_through_a_full_page_and_dedupes_the_boundary_row() -> None:
    # 100 rows, then the next page repeats the last row (``from`` is inclusive)
    # plus 2 genuinely new ones.
    first = [attack(i, T0 + i) for i in range(1, 101)]
    second = [attack(100, T0 + 100), attack(101, T0 + 101), attack(102, T0 + 102)]

    def route(path: str) -> dict[str, Any]:
        return attacks_page(*(first if _query(path)["from"] == str(T0) else second))

    fake = FakeTornClient({r"/attacks": route})
    hits = await _service(fake).list_attacks(OUR_ID, since=SINCE)

    assert [h.attack_id for h in hits] == list(range(1, 103))
    assert len(fake.calls) == 2
    assert _query(fake.calls[1])["from"] == str(T0 + 100)


async def test_stops_at_the_page_cap() -> None:
    def route(path: str) -> dict[str, Any]:
        start = int(_query(path)["from"]) - T0
        return attacks_page(*(attack(1000 + start + i, T0 + start + i) for i in range(1, 101)))

    fake = FakeTornClient({r"/attacks": route})
    hits = await _service(fake).list_attacks(OUR_ID, since=SINCE, max_pages=3)
    assert len(fake.calls) == 3
    assert len(hits) == 300


async def test_a_full_page_stuck_in_one_second_does_not_loop_forever() -> None:
    rows = [attack(i, T0) for i in range(1, 101)]
    fake = FakeTornClient({r"/attacks": attacks_page(*rows)})
    hits = await _service(fake).list_attacks(OUR_ID, since=SINCE, max_pages=5)
    assert len(hits) == 100
    assert len(fake.calls) == 1


async def test_a_key_that_cannot_read_the_feed_raises() -> None:
    def deny(path: str) -> dict[str, Any]:
        raise TornAPIAccessLevelError("Incorrect ID-entity relation")

    fake = FakeTornClient({r"/attacks": deny})
    with pytest.raises(TornAPIAccessLevelError):
        await _service(fake).list_attacks(OUR_ID, since=SINCE)


async def test_the_chain_number_is_parsed() -> None:
    fake = FakeTornClient(
        {r"/attacks": attacks_page(attack(1, T0 + 1, chain=380), attack(2, T0 + 2, chain=0))}
    )
    hits = await _service(fake).list_attacks(OUR_ID, since=SINCE)
    assert [h.chain for h in hits] == [380, 0]


async def test_the_warlord_bonus_is_parsed_and_absent_modifiers_mean_unknown() -> None:
    no_modifiers = attack(3, T0 + 3)
    no_modifiers["modifiers"] = None
    fake = FakeTornClient(
        {
            r"/attacks": attacks_page(
                attack(1, T0 + 1, warlord=1.18), attack(2, T0 + 2, warlord=1.0), no_modifiers
            )
        }
    )
    hits = await _service(fake).list_attacks(OUR_ID, since=SINCE)
    assert [h.warlord for h in hits] == [1.18, 1.0, None]
