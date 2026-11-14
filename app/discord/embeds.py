"""Discord embed builders (spec §15, §16, §21).

Kept separate from command handlers so the same embed can be produced for a
slash-command reply and for a live, edited-in-place channel message.
"""

from __future__ import annotations

from datetime import datetime

import discord

from app.database.models import ConfirmedStat, WarLogEvent
from app.torn.models import PlayerState
from app.utils.formatting import format_stat, truncate
from app.utils.time import london_clock, london_date, utcnow
from app.war.history import HistoricWar
from app.war.intelligence import Confidence, EstimateTrend
from app.war.roster import Roster
from app.war.score import WarScore
from app.war.spy import SPY_TRUSTED_FOR

BRAND_COLOUR = discord.Colour(0xB3202C)  # war red
ESTIMATE_DISCLAIMER = "Battle-strength figures are ESTIMATES, not confirmed stats."

_STATE_LABEL: dict[PlayerState, str] = {
    PlayerState.OKAY: "🟢 Okay",
    PlayerState.HOSPITAL: "🔴 Hospital",
    PlayerState.JAIL: "🟠 Jail",
    PlayerState.TRAVELING: "🔵 Travelling",
    PlayerState.UNKNOWN: "⚪ Unknown",
}

_CONFIDENCE_LABEL: dict[Confidence, str] = {
    Confidence.HIGH: "🟢 HIGH",
    Confidence.MEDIUM: "🟡 MEDIUM",
    Confidence.LOW: "🟠 LOW",
    Confidence.UNKNOWN: "⚪ UNKNOWN",
}


def base_embed(title: str, *, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, colour=BRAND_COLOUR)
    embed.set_footer(text=f"Torn War Intelligence • {london_clock(utcnow())} (Europe/London)")
    return embed


def torn_profile_url(player_id: int) -> str:
    return f"https://www.torn.com/profiles.php?XID={player_id}"


# A single embed description holds 4096 characters; stay a little under. The roster
# table's rows are fixed-width (~38 characters), so about 100 fit in one embed — and a
# Torn faction has at most 100 members, so a roster normally stays one unbroken list.
# Only a list that would overflow is split, across embeds in the same message.
_TABLE_CHAR_LIMIT = 4000


def war_info_embed(
    *,
    our_faction_name: str | None,
    enemy_faction_name: str | None,
    enemy_member_count: int,
    status: str,
    torn_war_id: int | None,
    war_start: datetime | None = None,
    war_end: datetime | None = None,
) -> discord.Embed:
    """The live war summary (spec §15)."""
    embed = base_embed("⚔️ RANKED WAR")
    embed.add_field(name="Your faction", value=our_faction_name or "—", inline=True)
    embed.add_field(name="Opposition", value=enemy_faction_name or "—", inline=True)
    embed.add_field(name="Enemy members", value=str(enemy_member_count), inline=True)
    embed.add_field(name="War status", value=status.replace("_", " "), inline=True)
    if torn_war_id is not None:
        embed.add_field(name="Torn war ID", value=str(torn_war_id), inline=True)
    if war_start is not None:
        embed.add_field(name="Started", value=london_clock(war_start), inline=True)
    if war_end is not None:
        embed.add_field(name="Ended", value=london_clock(war_end), inline=True)
    embed.add_field(name="Last updated", value=london_clock(utcnow()), inline=True)
    return embed


_SCORE_WIN_COLOUR = discord.Colour(0x2ECC71)  # green
_SCORE_TIED_COLOUR = discord.Colour(0x95A5A6)  # grey
_FINAL_LABEL: dict[str, str] = {"WIN": "🏆 War won", "LOSS": "🔴 War lost", "DRAW": "⚪ War drawn"}


def _points(value: int | None) -> str:
    return f"**{value:,}**" if value is not None else "**—**"


def _score_status_line(score: WarScore) -> str:
    lead = score.lead
    if score.finished:
        label = _FINAL_LABEL.get(score.result or "", "🏁 War over")
        return label if not lead else f"{label} by **{abs(lead):,}** points"
    if lead is None:
        return "Score unavailable right now."
    if lead > 0:
        return f"🟢 Leading by **{lead:,}**"
    if lead < 0:
        return f"🔴 Trailing by **{-lead:,}**"
    return "⚪ Tied"


def war_score_embed(score: WarScore) -> discord.Embed:
    """The small live-score box for ``#war-information``: both factions' points, the
    war's target, and who is ahead. Edited in place, so it stays one small message."""
    title = "🏁 FINAL SCORE" if score.finished else "📊 LIVE SCORE"
    embed = base_embed(title, description=_score_status_line(score))
    our_name = truncate(score.our_name or "Us", 100)
    enemy_name = truncate(score.enemy_name or "Enemy", 100)
    embed.add_field(name=our_name, value=_points(score.our_score))
    embed.add_field(name=enemy_name, value=_points(score.enemy_score))
    if score.target_score is not None:
        embed.add_field(name="Target", value=f"{score.target_score:,}")

    lead = score.lead
    if lead is None or lead == 0:
        embed.colour = _SCORE_TIED_COLOUR
    else:
        embed.colour = _SCORE_WIN_COLOUR if lead > 0 else BRAND_COLOUR
    return embed


def _paginate_rows(rows: list[str], *, fixed_chars: int) -> list[list[str]]:
    """Pack ``rows`` into as few embeds as fit under ``_TABLE_CHAR_LIMIT``.

    ``fixed_chars`` is what each embed spends on its code fence and column header.
    Rows are never dropped — a list that overflows is split, not cut off.
    """
    pages: list[list[str]] = [[]]
    size = fixed_chars
    for row in rows:
        if pages[-1] and size + len(row) + 1 > _TABLE_CHAR_LIMIT:
            pages.append([])
            size = fixed_chars
        pages[-1].append(row)
        size += len(row) + 1  # +1 for the newline
    return pages


def roster_embed(roster: Roster) -> list[discord.Embed]:
    """Enemy roster ranked by estimated strength (spec §16).

    One fixed-width table — every enemy always appears. It is split across embeds
    in one message only if it would overflow a single embed (see ``_TABLE_CHAR_LIMIT``).
    """
    entries = roster.sorted_by_strength()
    total = len(entries)

    # No status column on purpose: a player's status is a point-in-time snapshot that
    # is out of date between refreshes, and this table is about strength, not status.
    # #target-board still shows status (its ST column).
    header = f"{'PLAYER':<16}{'LVL':>4}  {'EST. BS':>9}  {'FF':>5}"
    rows = []
    for e in entries:
        name = truncate(e.name or str(e.player_id), 16)
        bs = format_stat(e.estimated_bs)
        ff = f"{e.fair_fight:.2f}" if e.fair_fight is not None else "—"
        lvl = str(e.level) if e.level is not None else "?"
        rows.append(f"{name:<16}{lvl:>4}  {bs:>9}  {ff:>5}")

    # Code fence (4 + 4 chars) plus the header and its underline, each with a newline.
    pages = _paginate_rows(rows, fixed_chars=8 + 2 * (len(header) + 1))
    title_base = f"🎯 Enemy roster — {roster.enemy_faction_name or roster.enemy_faction_id}"
    embeds = []
    shown = 0
    for i, page_rows in enumerate(pages, start=1):
        table = "```\n" + "\n".join([header, "-" * len(header), *page_rows]) + "\n```"
        title = title_base if len(pages) == 1 else f"{title_base} ({i}/{len(pages)})"
        embed = base_embed(title, description=table)
        if len(pages) == 1:
            note = f"{total} enemies, ranked by ESTIMATED strength."
        else:
            page_end = shown + len(page_rows)
            note = f"Enemies {shown + 1}-{page_end} of {total}, ranked by ESTIMATED strength."
        shown += len(page_rows)
        if not roster.estimates_available:
            note += " ⚠️ FFScouter unavailable — showing last known estimates."
        embed.add_field(name="​", value=note, inline=False)
        embed.set_footer(text=f"{ESTIMATE_DISCLAIMER} • {london_clock(utcnow())} Europe/London")
        embeds.append(embed)
    return embeds


_LOG_ICON: dict[str, str] = {
    "WAR_REGISTERED": "⚔️",
    "STATE_CHANGE": "🔄",
    "WAR_FINISHED": "🏁",
    "ROSTER_JOIN": "🆕",
    "ROSTER_LEFT": "👋",
    "OVERDOSE": "🤢",
    "HIT_FEED_BLOCKED": "⚠️",
}


def war_log_event_embed(event: WarLogEvent) -> discord.Embed:
    """One append-only line for ``#war-log`` (spec §32) — never edited in place."""
    icon = _LOG_ICON.get(event.kind, "📝")
    embed = discord.Embed(description=f"{icon} {event.message}", colour=BRAND_COLOUR)
    embed.set_footer(text=f"{london_clock(event.occurred_at)} Europe/London")
    return embed


def spy_report_text(spy: ConfirmedStat, *, now: datetime | None = None) -> str:
    """A stored spy report as a few lines: total, the four stats, who and when."""
    total = spy.total
    if total is None and None not in (spy.strength, spy.defense, spy.speed, spy.dexterity):
        total = sum(v or 0 for v in (spy.strength, spy.defense, spy.speed, spy.dexterity))
    lines = [f"**Total {format_stat(total)}**"]
    stats = (
        ("Str", spy.strength),
        ("Def", spy.defense),
        ("Spd", spy.speed),
        ("Dex", spy.dexterity),
    )
    if any(v is not None for _, v in stats):
        lines.append(" · ".join(f"{label} {format_stat(v)}" for label, v in stats))
    when = f"<t:{int(spy.reported_at.timestamp())}:R>"
    by = f" by <@{spy.reported_by_discord_id}>" if spy.reported_by_discord_id else ""
    lines.append(f"Reported {when}{by}")
    if spy.note:
        lines.append(f"_{discord.utils.escape_markdown(spy.note)}_")
    if (now or utcnow()) - spy.reported_at > SPY_TRUSTED_FOR:
        lines.append(f"⚠️ Over {SPY_TRUSTED_FOR.days} days old — the estimate is used for ranking.")
    return "\n".join(lines)


def player_intel_embed(
    *,
    name: str | None,
    player_id: int,
    level: int | None,
    state: PlayerState,
    trend: EstimateTrend,
    fair_fight: float | None,
    compact: bool = False,
    spy: ConfirmedStat | None = None,
) -> discord.Embed:
    """Player intelligence card (spec §21). ``compact=True`` drops status/FF for
    the lighter ``/stats`` reply. ``spy`` is this server's newest spy report, shown
    in its own field — never blended into the estimate figures."""
    title = name or f"Player {player_id}"
    embed = base_embed(f"⚔️ {title}")
    embed.url = torn_profile_url(player_id)
    if spy is not None:
        embed.add_field(name="🔍 Spy report (confirmed)", value=spy_report_text(spy), inline=False)

    if not trend.has_estimate:
        embed.description = (
            "No battle-strength estimate available yet for this player.\n"
            f"{ESTIMATE_DISCLAIMER}"
        )
        if level is not None:
            embed.add_field(name="Level", value=str(level), inline=True)
        embed.add_field(name="Status", value=_STATE_LABEL[state], inline=True)
        return embed

    est = format_stat(trend.current)
    prev = format_stat(trend.previous) if trend.previous is not None else "—"
    embed.add_field(
        name="Estimated Battle Strength", value=f"**{est}**  _(estimated)_", inline=False
    )
    embed.add_field(name="Previous", value=prev, inline=True)
    embed.add_field(name="Change", value=f"{trend.arrow} {trend.change_pct_label}", inline=True)
    embed.add_field(name="Estimate age", value=trend.age_label, inline=True)

    if not compact:
        if level is not None:
            embed.add_field(name="Level", value=str(level), inline=True)
        if fair_fight is not None:
            embed.add_field(name="Fair Fight", value=f"{fair_fight:.2f}", inline=True)
        embed.add_field(name="Status", value=_STATE_LABEL[state], inline=True)

    embed.add_field(
        name="Confidence",
        value=f"{_CONFIDENCE_LABEL[trend.confidence]}  ({trend.sample_count} samples)",
        inline=False,
    )
    embed.set_footer(text=f"{ESTIMATE_DISCLAIMER} • {london_clock(utcnow())} Europe/London")
    return embed


_RESULT_ICON: dict[str, str] = {"WIN": "🟢", "LOSS": "🔴", "DRAW": "⚪"}


def history_embed(wars: list[HistoricWar]) -> discord.Embed:
    """This server's past finished wars, most recent first (spec §22)."""
    if not wars:
        return base_embed(
            "📜 War history", description="No finished wars on record for this server yet."
        )

    header = f"{'ENDED':<10}  {'OPPONENT':<20}  RESULT"
    lines = [header, "-" * len(header)]
    for w in wars:
        ended = london_date(w.war_end) if w.war_end else "?"
        opponent = truncate(w.enemy_faction_name or "Unknown", 20)
        icon = _RESULT_ICON.get(w.result or "", "⚪")
        lines.append(f"{ended:<10}  {opponent:<20}  {icon} {w.result or 'UNKNOWN'}")

    table = "```\n" + "\n".join(lines) + "\n```"
    embed = base_embed("📜 War history", description=table)
    embed.add_field(name="​", value=f"Showing the last {len(wars)} finished war(s).", inline=False)
    return embed
