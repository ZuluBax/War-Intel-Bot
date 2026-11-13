"""The live target board (``#target-board``): one list of every enemy, grouped by what
they can do *right now* — attack them, wait for hospital, or wait for a flight.

Countdowns are Discord timestamps (``<t:…:R>``), which the reader's own Discord counts
down every second; the bot only re-renders when the status data is refreshed. That only
works in normal message text, so the board is one line per player, not a code-block table.

Each message is ONE embed (one box in Discord), so it reads as one unbroken list.
An embed's description holds only 4096 characters, so the overflow continues in extra
"fields" of the same embed — they flow on directly underneath, with no second box.

Discord caps a whole message at 6000 characters, which ~70 full-detail lines fill (a
war against 87 enemies, Nov 2026, was losing 17 rows). So a roster that doesn't fit in
one message is split into TWO messages with as near the same number of enemies each as
fits — full detail kept. Only if even two messages can't hold it are the lines
shortened, and only then cut from the bottom (with a note saying so).

ABROAD is split into one sub-list per country, A to Z; enemies flying out to a country
are listed under it too, below those already there. Discord messages cannot fold a
section open or shut (checked against Discord's component reference, 24 Sep 2026), and
the board is one message everyone shares — so "show me only Japan" is a drop-down under
the board (``app.discord.abroad_picker``) that answers with a private copy of one country.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import discord

from app.database.repositories import OverdoseView
from app.discord.alert_emoji import AlertEmoji
from app.discord.embeds import base_embed
from app.utils.formatting import format_stat, truncate
from app.utils.logging import get_logger
from app.utils.time import london_clock, utcnow
from app.war.hospital_watch import SURELY_REVIVED
from app.war.live_status import LiveStatus, StatusGroup, Window, live_status
from app.war.target_service import TargetView

logger = get_logger(__name__)

TARGET_BOARD_TITLE = "⚔️ TARGET BOARD"
_ONLINE = "Online"  # Torn's own label (last_action.status): Online / Idle / Offline
_ONLINE_MARK = "⚡"
_WINDOW_MARK = "🎯"
_MED_MARK = "💊"
SPY_MARK = "🔍"
BOARD_FOOTER = (
    f"{_ONLINE_MARK} = online now • "
    "Order inside Available is a heuristic, not a guarantee • "
    "~ = landing window worked out by the bot (Torn does not publish one) • "
    f"{_WINDOW_MARK} = FFScouter's landing window • "
    f"{_MED_MARK} = used meds to cut hospital time • "
    f"{SPY_MARK} = stats from a spy report"
)

# Discord limits: an embed description holds 4096 characters, a field value 1024, and the
# whole embed 6000 (title, description, field names and values, footer). Stay under each,
# leaving ~400 of the 6000 for the title, footer and field names.
_DESC_LIMIT = 4000
_FIELD_LIMIT = 1000
_TOTAL_BUDGET = 5600
_FIELD_NAME = "\u200b"  # zero-width space: Discord rejects an empty field name

_ICON: dict[StatusGroup, str] = {
    StatusGroup.AVAILABLE: "🟢",
    StatusGroup.HOSPITAL: "🔴",
    StatusGroup.INCOMING: "🛬",
    StatusGroup.ABROAD: "🌍",
    StatusGroup.OUTGOING: "🛫",
    StatusGroup.UNKNOWN: "⚪",
}
_HEADER: dict[StatusGroup, str] = {
    StatusGroup.AVAILABLE: "🟢 **AVAILABLE — attack now**",
    StatusGroup.HOSPITAL: "🔴 **HOSPITAL / JAIL — out soonest first**",
    StatusGroup.INCOMING: "🛬 **LANDING IN TORN — soonest first**",
    StatusGroup.ABROAD: "🌍 **ABROAD**",
    StatusGroup.OUTGOING: "🛫 **FLYING OUT**",
    StatusGroup.UNKNOWN: "⚪ **STATUS UNKNOWN**",
}


# Torn's own spelling of every destination, as stored from "In <country>" (checked
# against every abroad status the live database holds, 24 Sep 2026).
_FLAG: dict[str, str] = {
    "argentina": "🇦🇷",
    "canada": "🇨🇦",
    "cayman islands": "🇰🇾",
    "china": "🇨🇳",
    "hawaii": "🌺",  # a US state: no flag of its own, and 🇺🇸 would read as "USA"
    "japan": "🇯🇵",
    "mexico": "🇲🇽",
    "south africa": "🇿🇦",
    "switzerland": "🇨🇭",
    "uae": "🇦🇪",
    "united kingdom": "🇬🇧",
}


def country_flag(place: str) -> str:
    return _FLAG.get(place.casefold(), "🌍")  # a destination Torn adds later still shows


def _country_header(place: str, here: int, flying: int) -> str:
    count = f"{here} + {flying} flying in" if flying else str(here)
    return f"{country_flag(place)} __**{place}**__ ({count})"


def attack_url(player_id: int) -> str:
    # Torn retired loader.php (it now answers "no longer available ... use page.php").
    # This is the address of the Attack button on a Torn profile page, read from the page.
    return f"https://www.torn.com/page.php?sid=attack&user2ID={player_id}"


def _ts(when: datetime, style: str) -> str:
    return f"<t:{int(when.timestamp())}:{style}>"


def _name(view: TargetView) -> str:
    raw = truncate(view.name or str(view.player_id), 20)
    # Brackets would end the link text early; markdown characters would style the name.
    return discord.utils.escape_markdown(raw.replace("[", "").replace("]", ""))


def _when(when: datetime, *, compact: bool) -> str:
    """A live countdown plus, unless space is tight, the clock time it lands on."""
    return _ts(when, "R") if compact else f"{_ts(when, 'R')} ({_ts(when, 't')})"


def _detail(status: LiveStatus, view: TargetView, now: datetime, *, compact: bool) -> str:
    group = status.group
    if group is StatusGroup.HOSPITAL:
        verb = "out of jail" if status.jailed else "out"
        if status.release_at is None:
            return f"{verb} ?"
        if status.release_at <= now:
            return f"{verb} now"
        return f"{verb} {_when(status.release_at, compact=compact)}"
    if group is StatusGroup.OUTGOING and _country(status) is not None:
        # Listed under its destination's header, so the route would only repeat it.
        return f"flying in · {_landing(status, now, compact=compact)}"
    if group in (StatusGroup.INCOMING, StatusGroup.OUTGOING):
        route = (
            f"{status.place} → Torn" if group is StatusGroup.INCOMING else f"Torn → {status.place}"
        )
        return f"{route} · {_landing(status, now, compact=compact)}"
    if group is StatusGroup.ABROAD:
        return ""  # the country is the sub-header this row sits under
    if group is StatusGroup.UNKNOWN:
        return truncate(view.status_description or "status unknown", 40)
    return ""


def _open_windows(status: LiveStatus, now: datetime) -> list[Window]:
    """Landing windows that have not fully passed. An airliner's BCT window drops out
    once it is over without a landing: then it must be a normal ticket."""
    return [w for w in status.windows if w.latest > now]


def _landing(status: LiveStatus, now: datetime, *, compact: bool) -> str:
    """``lands ~in 12 min (12:10-12:14) PI``; an airliner we can't pin down gets a second
    window (``… BCT or 14:02-14:20 Airline``). ``~`` = our own maths, 🎯 = FFScouter's."""
    if not status.windows:
        return "landing time unknown"
    mark = _WINDOW_MARK if status.from_ffscouter else "~"
    still = _open_windows(status, now)
    if not still:
        return f"landing now {mark}"
    first = still[0]
    if first.earliest <= now:
        text = f"landing now {mark} (by {_ts(first.latest, 't')})"
    elif compact:
        text = f"lands {mark}{_ts(first.earliest, 'R')}"
    else:
        text = (
            f"lands {mark}{_ts(first.earliest, 'R')} "
            f"({_ts(first.earliest, 't')}-{_ts(first.latest, 't')})"
        )
    if not compact and first.method != "Unknown":
        text += f" {first.method}"
    if len(still) > 1:
        other = still[1]
        text += f" or {_ts(other.earliest, 't')}-{_ts(other.latest, 't')}"
        if not compact:
            text += f" {other.method}"
    return text


# Sections whose names link to the attack page: attackable now, or abroad (where you can
# attack them if you are in the same country). Hospital and flying players cannot be attacked.
_CLICKABLE = (StatusGroup.AVAILABLE, StatusGroup.ABROAD)


_WARNING, _SIREN = 1, 2


def _alert_level(view: TargetView, status: LiveStatus) -> int:
    """0 = nothing; _WARNING = one medical cut this stay; _SIREN = two or more cuts,
    or just left hospital early (revived / healed out). Shown as animated emoji
    (app.discord.alert_emoji)."""
    if status.group is StatusGroup.HOSPITAL and view.med_drops:
        return _SIREN if view.med_drops >= 2 else _WARNING
    if status.group is StatusGroup.AVAILABLE and view.out_early_at is not None:
        return _SIREN
    return 0


def _minutes(delta: timedelta) -> str:
    mins = round(delta.total_seconds() / 60)
    return f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m"


def _alert_note(view: TargetView, status: LiveStatus) -> str | None:
    if status.group is StatusGroup.HOSPITAL and view.med_drops:
        times = f"x{view.med_drops} " if view.med_drops >= 2 else ""
        return f"**{_MED_MARK} {times}-{_minutes(view.med_cut)}**"
    if status.group is StatusGroup.AVAILABLE and view.out_early_at is not None:
        # With less left, one big medical item could have done it: don't claim a revive.
        what = "REVIVED" if view.out_early_by >= SURELY_REVIVED else "OUT EARLY"
        return f"**{what} {_ts(view.out_early_at, 'R')} ({_minutes(view.out_early_by)} early)**"
    return None


def _row(view: TargetView, status: LiveStatus, now: datetime, *, compact: bool) -> str:
    name = _name(view)
    if status.group in _CLICKABLE and view.player_id > 0:
        name = f"[{name}]({attack_url(view.player_id)})"  # click the name to attack
    icon = _ICON[status.group]
    level = _alert_level(view, status)
    if level:
        icon = f"{AlertEmoji.siren if level == _SIREN else AlertEmoji.warning}{icon}"
    if view.online_status == _ONLINE:
        # Bold + a marker: Discord text cannot be coloured, and bold alone is easy to miss.
        name = f"**{name}**"
        icon += _ONLINE_MARK
    parts = [f"{icon} {name}", f"L{view.level if view.level is not None else '?'}"]
    if not compact:
        ff = f"FF {view.fair_fight:.2f}" if view.fair_fight is not None else "FF ?"
        stat = format_stat(view.estimated_bs)
        parts += [f"{stat}{SPY_MARK}" if view.confirmed else stat, ff]
    detail = _detail(status, view, now, compact=compact)
    if detail:
        parts.append(detail)
    note = _alert_note(view, status)
    if note:
        parts.append(note)
    return " · ".join(parts)


# Torn's last_action.status, most attackable-right-now first. Anything else (missing /
# unexpected) sorts with Offline.
_ACTIVITY_RANK = {_ONLINE: 0, "Idle": 1}
# Where someone online is worth moving up: they can be hit now (or once you fly there).
_ONLINE_FIRST = (StatusGroup.AVAILABLE, StatusGroup.ABROAD)


def _activity(view: TargetView, status: LiveStatus) -> int:
    if status.group not in _ONLINE_FIRST:
        return 0
    return _ACTIVITY_RANK.get(view.online_status or "", 2)


def _order(
    view: TargetView, status: LiveStatus, now: datetime
) -> tuple[int, str, bool, int, bool, float, str, int, int]:
    """Section, then (in ABROAD) country A to Z with those already there above those
    flying in, then alerts (siren before warning) at the top of their group, then
    soonest first (unknown times last), then by place, then — in AVAILABLE and each
    ABROAD country — online, idle, offline, then the priority ranking, so the best
    target among those online is on top."""
    if status.group is StatusGroup.HOSPITAL:
        when = status.release_at
    else:
        still = _open_windows(status, now)
        when = still[0].earliest if still else status.arrival_at
    group, country = _section(status)
    return (
        int(group),
        (country or "").casefold(),
        status.group is StatusGroup.OUTGOING,
        -_alert_level(view, status),
        when is None,
        when.timestamp() if when is not None else 0.0,
        (status.place or "").casefold(),
        _activity(view, status),
        view.rank,
    )


Items = list[tuple[TargetView, LiveStatus]]
Section = tuple[str, list[str]]  # (header, rows)
Sections = list[Section]


def _country(status: LiveStatus) -> str | None:
    """The country an enemy is in, or flying out to (listed under it so you can see who
    will be there soon); None for every other group."""
    if status.group is StatusGroup.ABROAD:
        return status.place or "?"
    if status.group is StatusGroup.OUTGOING:
        return status.place  # no destination -> stays in FLYING OUT
    return None


def _section(status: LiveStatus) -> tuple[StatusGroup, str | None]:
    """Which board section a row sits in: flights out join their country under ABROAD."""
    country = _country(status)
    return (StatusGroup.ABROAD, country) if country is not None else (status.group, None)


def _sections(
    items: Items, now: datetime, compact: bool, *, counts_from: Items | None = None
) -> Sections:
    """One section per group; ABROAD gets one per country (``_order`` already sorts
    them A to Z), the first of them also carrying the ABROAD header. Header counts come
    from ``counts_from`` (the whole board) when ``items`` is only one part of it."""
    group_counts: dict[StatusGroup, int] = {}
    here: dict[str, int] = {}
    flying: dict[str, int] = {}
    for _, status in counts_from if counts_from is not None else items:
        group, country = _section(status)
        group_counts[group] = group_counts.get(group, 0) + 1
        if country is not None:
            tally = flying if status.group is StatusGroup.OUTGOING else here
            tally[country] = tally.get(country, 0) + 1

    sections: Sections = []
    current: tuple[StatusGroup, str | None] | None = None
    for view, status in items:
        key = _section(status)
        if key != current:
            lines = []
            if current is None or current[0] is not key[0]:
                lines.append(f"{_HEADER[key[0]]} ({group_counts[key[0]]})")
            if key[1] is not None:
                lines.append(_country_header(key[1], here.get(key[1], 0), flying.get(key[1], 0)))
            sections.append(("\n".join(lines), []))
            current = key
        sections[-1][1].append(_row(view, status, now, compact=compact))
    return sections


def _chunk(sections: list[tuple[str, list[str]]], footer_note: str | None) -> list[str]:
    """Pack section headers and rows into text chunks: the first fills the embed
    description (``_DESC_LIMIT``), the rest fill fields (``_FIELD_LIMIT``). Splits only
    between rows, so the list simply continues from one chunk into the next."""
    chunks: list[list[str]] = [[]]
    size = 0

    def limit() -> int:
        return _DESC_LIMIT if len(chunks) == 1 else _FIELD_LIMIT

    def push(line: str) -> None:
        nonlocal size
        chunks[-1].append(line)
        size += len(line) + 1  # +1 for the newline

    def next_chunk() -> None:
        nonlocal size
        chunks.append([])
        size = 0

    for header, rows in sections:
        if chunks[-1]:
            push("")  # blank line between sections
        # A header is never left stranded at the end of a chunk: it must fit with its first row.
        if chunks[-1] and size + len(header) + len(rows[0]) + 2 > limit():
            next_chunk()
        push(header)
        for row in rows:
            if size + len(row) + 1 > limit():
                next_chunk()
            push(row)
    if footer_note:
        if size + len(footer_note) + 2 > limit():
            next_chunk()
        push("")
        push(footer_note)
    return ["\n".join(chunk).strip() for chunk in chunks]


def target_board_cleared_embed(*, now: datetime | None = None) -> discord.Embed:
    """Replaces the live list once the war ends: the targets on it are no longer
    valid attack advice, so the board goes blank until the next war starts."""
    now = now or utcnow()
    embed = base_embed(
        TARGET_BOARD_TITLE,
        description="War over — this board clears until the next war starts.",
    )
    embed.set_footer(text=f"{london_clock(now)} Europe/London")
    return embed


def _ordered(targets: list[TargetView], now: datetime) -> Items:
    return sorted(
        (
            (
                t,
                live_status(
                    t.state,
                    t.status_description,
                    t.status_until,
                    t.plane_type,
                    t.status_since,
                    t.flight,
                    t.airliner_method,
                ),
            )
            for t in targets
        ),
        key=lambda pair: _order(pair[0], pair[1], now),
    )


_CONTINUED = " — continued"
# A roster bigger than one message is split over at most this many.
_MAX_MESSAGES = 2


def _parts(
    items: Items, cut: int | None, now: datetime, compact: bool, tail: Section | None
) -> list[Sections]:
    """The board's sections as one part (``cut`` None) or two parts split before row
    ``cut``. A section cut in two repeats its header in part 2, marked continued, so
    part 2 never opens on rows with no heading. ``tail`` (overdoses) ends the last part."""
    pieces = [items] if cut is None else [items[:cut], items[cut:]]
    parts = [_sections(piece, now, compact, counts_from=items) for piece in pieces]
    # Same group either side of the cut (in ABROAD, even if the country changes there).
    if cut is not None and _section(items[cut - 1][1])[0] is _section(items[cut][1])[0]:
        header, rows = parts[1][0]
        first, *rest = header.split("\n")
        parts[1][0] = ("\n".join([first + _CONTINUED, *rest]), rows)
    if tail is not None:
        parts[-1].append(tail)
    return parts


def _fits(messages: list[list[str]]) -> bool:
    return all(sum(len(c) for c in chunks) <= _TOTAL_BUDGET for chunks in messages)


def _cuts(n: int) -> list[int]:
    """Where to split n rows in two, most even first: the middle, then one either side,
    two either side… (a half of long hospital lines may need the cut moved a little).
    Only near the middle: the halves should stay roughly equal, and every try is a
    full re-render on a board refreshed every 15 seconds."""
    mid = (n + 1) // 2
    order = [mid]
    for step in range(1, max(2, n // 10) + 1):
        order += [c for c in (mid - step, mid + step) if 0 < c < n]
    return order


def _fitted(
    items: Items,
    now: datetime,
    *,
    empty: str,
    tail: Section | None = None,
    max_messages: int = 1,
) -> list[list[str]]:
    """Text chunks for each message. Tries, in order: full lines in one message; full
    lines split as evenly as fits over two; shorter lines (one, then two messages); and
    only then drops the last (least urgent) rows, saying so."""
    if not items:
        return [[empty]]

    def render(rows: Items, compact: bool, note: str | None) -> list[list[str]] | None:
        cuts: list[int | None] = [None]
        if max_messages > 1 and len(rows) > 1:
            cuts += _cuts(len(rows))
        for cut in cuts:
            parts = _parts(rows, cut, now, compact, tail)
            messages = [
                _chunk(sections, note if i == len(parts) - 1 else None)
                for i, sections in enumerate(parts)
            ]
            if _fits(messages):
                return messages
        return None

    for compact in (False, True):
        fitted = render(items, compact, None)
        if fitted is not None:
            return fitted
    def trimmed(kept: int) -> list[list[str]] | None:
        note = f"…and {len(items) - kept} more not shown (roster too large for the board)"
        return render(items[:kept], True, note)

    # Most rows that still fit, by halving (fewer rows only ever get shorter, so this
    # finds the boundary in ~log2(n) renders instead of one per dropped row).
    low, high, best = 1, len(items) - 1, None
    while low <= high:
        mid = (low + high) // 2
        fitted = trimmed(mid)
        if fitted is not None:
            best, low = (mid, fitted), mid + 1
        else:
            high = mid - 1
    shown = best[0] if best else 0
    logger.warning("target_board.trimmed", total=len(items), shown=shown)
    if best is None:
        return [[f"…{len(items)} enemies not shown (roster too large for the board)"]]
    return best[1]


def _embed(title: str, chunks: list[str], now: datetime) -> discord.Embed:
    embed = base_embed(title, description=chunks[0])
    for chunk in chunks[1:]:
        embed.add_field(name=_FIELD_NAME, value=chunk, inline=False)
    embed.set_footer(text=f"{BOARD_FOOTER} • {london_clock(now)} Europe/London")
    return embed


def _embeds(title: str, messages: list[list[str]], now: datetime) -> list[discord.Embed]:
    """One embed per message; a split board's parts are titled ``(1/2)``, ``(2/2)``."""
    if len(messages) == 1:
        return [_embed(title, messages[0], now)]
    total = len(messages)
    return [
        _embed(f"{title} ({i}/{total})", chunks, now) for i, chunks in enumerate(messages, 1)
    ]


_OVERDOSE_HEADER = "🤢 **OVERDOSED SINCE THE WAR WAS ANNOUNCED**"


def _overdose_row(od: OverdoseView, now: datetime) -> str:
    raw = truncate(od.name or str(od.torn_player_id), 20)
    name = discord.utils.escape_markdown(raw.replace("[", "").replace("]", ""))
    if od.hospital_until is None:
        when = "in hospital"
    elif od.hospital_until > now:
        when = f"out {_ts(od.hospital_until, 'R')}"
    else:
        when = "out of hospital"
    return f"🤢 {name} — {truncate(od.drug, 16)} · {when}"


def target_board_embed(
    targets: list[TargetView],
    *,
    overdoses: Sequence[OverdoseView] = (),
    now: datetime | None = None,
) -> list[discord.Embed]:
    """The board as ONE EMBED PER MESSAGE: usually one; two when the roster is too big
    for one message (``WarBoard.publish_messages`` posts each as its own message)."""
    now = now or utcnow()
    tail: Section | None = None
    if overdoses and targets:
        # Counted in the size budget: the enemy list makes room for it, never the
        # other way round. It always ends the last message.
        tail = (
            f"{_OVERDOSE_HEADER} ({len(overdoses)})",
            [_overdose_row(od, now) for od in overdoses],
        )
    messages = _fitted(
        _ordered(targets, now),
        now,
        empty="No enemy roster yet.",
        tail=tail,
        max_messages=_MAX_MESSAGES,
    )
    return _embeds(TARGET_BOARD_TITLE, messages, now)


def abroad_countries(
    targets: list[TargetView], *, now: datetime | None = None
) -> list[tuple[str, int]]:
    """``[(country, enemies there or flying there)]`` A to Z: the choices for the
    country drop-down."""
    now = now or utcnow()
    counts: dict[str, int] = {}
    for _, status in _ordered(targets, now):
        place = _country(status)
        if place is not None:
            counts[place] = counts.get(place, 0) + 1
    return sorted(counts.items(), key=lambda pair: pair[0].casefold())


def country_embed(
    targets: list[TargetView], country: str, *, now: datetime | None = None
) -> list[discord.Embed]:
    """Only the enemies in (or flying to) one country: the private reply to the drop-down."""
    now = now or utcnow()
    items = [
        (v, s)
        for v, s in _ordered(targets, now)
        if (_country(s) or "").casefold() == country.casefold()
    ]
    flag = country_flag(country)
    # A private reply, so always one message (one country is far below the limit).
    messages = _fitted(
        items, now, empty=f"{flag} No enemies are in or flying to {country} right now."
    )
    return _embeds(f"{flag} ENEMIES IN {country.upper()}", messages, now)
