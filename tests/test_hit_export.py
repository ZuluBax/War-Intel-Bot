"""The paste-ready raw-hits CSV, in the payout sheet's own layout."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import WarHit
from app.war.hit_summary import hits_csv, summary_filename

T0 = datetime(2026, 9, 19, 18, 0, 0, tzinfo=UTC)


def _hit(
    hit_id: int,
    offset_s: int,
    *,
    attacker: str | None = "Alice",
    defender: str | None = "Bob",
    result: str = "Attacked",
    respect: float | None = 2.87,
    chain: int | None = 1,
    warlord: float | None = 1.15,
) -> WarHit:
    return WarHit(
        id=hit_id,
        war_id=1,
        torn_attack_id=hit_id,
        started_at=T0 + timedelta(seconds=offset_s),
        attacker_name=attacker,
        defender_name=defender,
        result=result,
        respect_gain=respect,
        chain=chain,
        warlord=warlord,
        is_war_hit=True,
        is_outgoing=True,
    )


def _rows(hits: list[WarHit]) -> list[list[str]]:
    return list(csv.reader(io.StringIO(hits_csv(hits).decode("utf-8-sig"))))


def test_header_matches_the_payout_sheets_raw_columns_exactly() -> None:
    assert _rows([])[0] == ["Attacker", "Defender", "Result", "Respect", "#Chain", "RW?", "Timestamp"]


def test_a_row_per_hit_with_the_sheets_yes_no_style() -> None:
    rows = _rows([_hit(1, 0, attacker="Deylite", defender="monzi999", result="Hospitalized",
                       respect=2.87, chain=380, warlord=1.0)])
    assert rows[1] == ["Deylite", "monzi999", "Hospitalized", "2.87", "380", "no", "0:00"]


def test_newest_hit_first_like_the_sheet() -> None:
    hits = [_hit(1, 0, defender="first"), _hit(2, 30, defender="second"), _hit(3, 60, defender="third")]
    assert [r[1] for r in _rows(hits)[1:]] == ["third", "second", "first"]


def test_timestamp_is_the_gap_since_the_previous_chain_hit_as_m_ss() -> None:
    hits = [
        _hit(1, 0, chain=1),  # first chain hit -> 0:00
        _hit(2, 103, chain=2),  # 1 min 43 s after it
        _hit(3, 110, chain=0, result="Lost", respect=0.0),  # not a chain hit -> 0:00
        _hit(4, 120, chain=3),  # 17 s after chain hit 2 (the Lost hit does not reset it)
        _hit(5, 125, chain=None, result="Assist"),  # unchained -> 0:00
    ]
    by_id = {int(r[4] or 0): r for r in _rows(hits)[1:]}
    rows = _rows(hits)[1:]
    assert [r[6] for r in rows] == ["0:00", "0:17", "0:00", "1:43", "0:00"]  # newest first
    assert by_id[3][6] == "0:17"


def test_a_long_gap_rolls_minutes_past_60_without_breaking() -> None:
    rows = _rows([_hit(1, 0, chain=1), _hit(2, 62 * 60 + 5, chain=2)])
    assert rows[1][6] == "62:05"


def test_respect_is_written_the_way_the_sheet_holds_it() -> None:
    cells = {
        r[1]: r[3]
        for r in _rows([
            _hit(1, 0, defender="a", respect=10.0),
            _hit(2, 1, defender="b", respect=0.0),
            _hit(3, 2, defender="c", respect=4.68),
            _hit(4, 3, defender="d", respect=None),
        ])[1:]
    }
    assert cells == {"a": "10", "b": "0", "c": "4.68", "d": ""}


def test_a_hit_stored_before_the_chain_column_existed_leaves_that_cell_blank() -> None:
    [_, row] = _rows([_hit(1, 0, chain=None)])
    assert row[4] == ""
    assert row[5] == "yes"  # RW? never depends on the chain number


def test_rw_is_yes_only_when_a_warlord_weapon_was_used() -> None:
    hits = [
        _hit(1, 0, warlord=1.18),
        _hit(2, 1, warlord=1.0),
        _hit(3, 2, warlord=None),  # stored before the bonus was kept: unknown, not "no"
    ]
    assert [r[5] for r in _rows(hits)[1:]] == ["", "no", "yes"]  # newest first


@pytest.mark.parametrize("name", ["=HYPERLINK(\"http://x\")", "+1", "-2", "@SUM(A1)"])
def test_names_a_spreadsheet_would_run_as_formulas_are_neutralised(name: str) -> None:
    [_, row] = _rows([_hit(1, 0, attacker=name, defender=name)])
    assert row[0] == "'" + name and row[1] == "'" + name


def test_starts_with_a_bom_so_excel_reads_it() -> None:
    assert hits_csv([]).startswith(b"\xef\xbb\xbf")


def test_filename_uses_the_hits_prefix() -> None:
    assert (
        summary_filename("Null & Void", "Agents of Truth", T0, prefix="war-hits")
        == "war-hits-null-void-vs-agents-of-truth-2026-09-19.csv"
    )
