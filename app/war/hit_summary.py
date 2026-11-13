"""End-of-war scoreboard: per-player hit counts from ``war_hits``.

Pure — no I/O — so the counting rules are easy to unit test. Callers pass in the
war hits to count (our faction's own war hits; see ``WarHitRepository.war_hits``).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.database.models import WarHit
from app.utils.formatting import sanitise_channel_name
from app.utils.time import london_date


class Outcome(StrEnum):
    WON = "won"
    DRAW = "draw"
    LOST = "lost"
    ASSIST = "assist"
    OTHER = "other"  # interrupted, or a result we have not seen and will not guess at


# Torn's attack ``result`` strings, seen in live data across three wars (843 hits,
# 2026-09-19): Attacked, Hospitalized, Mugged, Lost, Stalemate, Assist, Interrupted.
# ``Arrested`` is documented by Torn and unambiguous (the attacker jailed them).
# Anything else — Escape, Timeout, Special — is left as OTHER on purpose: it still
# counts as a hit, but is not guessed into a win or a loss.
_OUTCOME_BY_RESULT: dict[str, Outcome] = {
    "attacked": Outcome.WON,
    "hospitalized": Outcome.WON,
    "mugged": Outcome.WON,
    "arrested": Outcome.WON,
    "stalemate": Outcome.DRAW,
    "lost": Outcome.LOST,
    "assist": Outcome.ASSIST,
}


def outcome_of(result: str) -> Outcome:
    return _OUTCOME_BY_RESULT.get(result.strip().lower(), Outcome.OTHER)


@dataclass
class PlayerHitSummary:
    name: str
    player_id: int | None = None
    hits: int = 0  # every attack, so it also covers assists and OTHER results
    won: int = 0
    draw: int = 0
    lost: int = 0
    assist: int = 0

    @property
    def other(self) -> int:
        """Hits that are none of won/draw/lost/assist (e.g. interrupted)."""
        return self.hits - self.won - self.draw - self.lost - self.assist

    def add(self, outcome: Outcome) -> None:
        self.hits += 1
        if outcome is Outcome.WON:
            self.won += 1
        elif outcome is Outcome.DRAW:
            self.draw += 1
        elif outcome is Outcome.LOST:
            self.lost += 1
        elif outcome is Outcome.ASSIST:
            self.assist += 1


def summarise_hits(hits: Sequence[WarHit]) -> list[PlayerHitSummary]:
    """One row per attacker, busiest first (ties broken by name)."""
    players: dict[int | str, PlayerHitSummary] = {}
    # Oldest first, so a player who changed name mid-war shows under the newest.
    for hit in sorted(hits, key=lambda h: (h.started_at, h.id)):
        key: int | str = (
            hit.attacker_id if hit.attacker_id is not None else hit.attacker_name or "?"
        )
        row = players.get(key)
        if row is None:
            row = players[key] = PlayerHitSummary(
                name=hit.attacker_name or "Unknown", player_id=hit.attacker_id
            )
        elif hit.attacker_name:
            row.name = hit.attacker_name
        row.add(outcome_of(hit.result))
    return sorted(players.values(), key=lambda p: (-p.hits, p.name.lower()))


# A spreadsheet cell starting with one of these is run as a formula by Excel /
# Google Sheets. Torn names are letters, digits and underscores so this should
# never fire, but the cell text comes from an outside API, so it is neutralised.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(text: str) -> str:
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def summary_csv(players: Sequence[PlayerHitSummary]) -> bytes:
    """The scoreboard as a CSV that opens directly in Google Sheets or Excel.

    UTF-8 with a BOM, so Excel reads non-ASCII names correctly. ``Other`` is
    included (the Discord table omits it) so the columns always add up to Hits.
    """
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Player", "Player ID", "Hits", "Won", "Draw", "Lost", "Assist", "Other"])
    for p in players:
        writer.writerow(
            [
                _safe_cell(p.name),
                "" if p.player_id is None else p.player_id,
                p.hits,
                p.won,
                p.draw,
                p.lost,
                p.assist,
                p.other,
            ]
        )
    return out.getvalue().encode("utf-8-sig")


def summary_filename(
    our_faction_name: str | None,
    enemy_faction_name: str | None,
    when: datetime,
    *,
    prefix: str = "war-summary",
) -> str:
    def slug(name: str | None, fallback: str) -> str:
        return sanitise_channel_name(name or fallback, fallback=fallback)[:30].strip("-")

    return (
        f"{prefix}-{slug(our_faction_name, 'us')}-vs-"
        f"{slug(enemy_faction_name, 'enemy')}-{london_date(when)}.csv"
    )


def _respect_cell(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _gap_cell(seconds: float) -> str:
    """``m:ss`` — typed the way the payout sheet holds it (a Sheets import reads
    ``1:43`` as a time value exactly as the sheet's own cells do)."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def hits_csv(hits: Sequence[WarHit]) -> bytes:
    """Every hit as rows in the payout sheet's raw layout, newest first.

    Columns: ``Attacker, Defender, Result, Respect, #Chain, RW?, Timestamp`` — pasted
    into columns A-G of a war tab, the sheet's own formulas take over.

    ``Timestamp`` is the gap since the previous *chain* hit (chronologically), as
    ``m:ss``; ``0:00`` for the first chain hit and for hits that are not chain hits
    (lost / assist / unchained). ``RW?`` is ``yes`` when a ranked war weapon was used —
    Torn never names the weapon, but ranked-war weapons carry the Warlord bonus, which
    shows up as a respect modifier above 1. Blank if the bonus was never recorded.
    """
    ordered = sorted(hits, key=lambda h: (h.started_at, h.id))
    gaps: dict[int, str] = {}
    previous_chain_hit: WarHit | None = None
    for hit in ordered:
        if hit.chain and hit.chain > 0:
            gap = 0.0
            if previous_chain_hit is not None:
                gap = (hit.started_at - previous_chain_hit.started_at).total_seconds()
            gaps[hit.id] = _gap_cell(gap)
            previous_chain_hit = hit
        else:
            gaps[hit.id] = "0:00"

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Attacker", "Defender", "Result", "Respect", "#Chain", "RW?", "Timestamp"])
    for hit in reversed(ordered):
        rw = "" if hit.warlord is None else ("yes" if hit.warlord > 1 else "no")
        writer.writerow(
            [
                _safe_cell(hit.attacker_name or "Unknown"),
                _safe_cell(hit.defender_name or "Unknown"),
                hit.result,
                _respect_cell(hit.respect_gain),
                "" if hit.chain is None else hit.chain,
                rw,
                gaps[hit.id],
            ]
        )
    return out.getvalue().encode("utf-8-sig")
