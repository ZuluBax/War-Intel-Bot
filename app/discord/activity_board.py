"""The ``#enemy-activity`` post: when the enemy is busy and when it is quiet.

One live embed, edited in place. The hour chart is a code block so the bars line up;
everything is in TCT (= UTC), the clock Torn players plan wars in.
"""

from __future__ import annotations

from datetime import datetime

import discord

from app.discord.embeds import base_embed
from app.utils.formatting import format_stat, truncate
from app.utils.time import london_clock, utcnow
from app.war.activity import ActivitySummary, hour_ranges

ACTIVITY_TITLE = "🕒 ENEMY ACTIVITY"
_BAR_WIDTH = 14
_FIELD_LIMIT = 1000


def _bar(value: float, top: float) -> str:
    """A bar of up to ``_BAR_WIDTH`` blocks, in eighths so small differences show."""
    if top <= 0:
        return ""
    eighths = round(value / top * _BAR_WIDTH * 8)
    full, part = divmod(eighths, 8)
    return "█" * full + ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")[part]


def _chart(summary: ActivitySummary, now: datetime) -> str:
    top = max((h.avg_active or 0.0) for h in summary.hours)
    lines = ["TCT  avg enemies active"]
    for h in summary.hours:
        here = " ◀ now" if h.hour == now.hour else ""
        if h.avg_active is None:
            lines.append(f"{h.hour:02d}   · not watched yet{here}")
        else:
            lines.append(
                f"{h.hour:02d} {_bar(h.avg_active, top):<{_BAR_WIDTH}} {h.avg_active:4.1f}{here}"
            )
    return "```\n" + "\n".join(lines) + "\n```"


def _hours(hours: tuple[int, ...]) -> str:
    return ", ".join(f"{h:02d}:00" for h in hours)


def _watched(summary: ActivitySummary) -> str:
    total = round(summary.watched_hours)
    days, hours = divmod(total, 24)
    span = f"{days}d {hours}h" if days else f"{hours}h"
    since = (
        f" since {summary.watched_since:%a %H:%M} TCT" if summary.watched_since is not None else ""
    )
    return f"Watched for {span}{since}."


def _players(summary: ActivitySummary) -> str:
    lines: list[str] = []
    for p in summary.players:
        name = discord.utils.escape_markdown(truncate(p.member.name or str(p.member.player_id), 20))
        when = hour_ranges(p.usual_hours) or "no regular hours yet"
        lines.append(f"**{name}** ({format_stat(p.member.estimated_bs)}) · {when}")
    text = "\n".join(lines) or "No players yet."
    return truncate(text, _FIELD_LIMIT)


def activity_embed(summary: ActivitySummary, *, now: datetime | None = None) -> discord.Embed:
    now = now or utcnow()
    head = [f"**Active right now:** {summary.active_now} of {summary.member_count}"]
    if summary.busiest:
        head.append(f"🔥 **Busiest:** {_hours(summary.busiest)}")
        head.append(f"😴 **Quietest:** {_hours(summary.quietest)}")
    head.append(_watched(summary))
    if summary.learning:
        head.append("_Still learning — patterns firm up after about half a day of watching._")
    embed = base_embed(ACTIVITY_TITLE, description="\n".join(head) + "\n" + _chart(summary, now))
    embed.add_field(
        name="Strongest enemies — usually online (TCT)",
        value=_players(summary),
        inline=False,
    )
    embed.set_footer(
        text="Active = did anything in Torn in a 5-min slot • Times are TCT • "
        f"Updated every 15 min • {london_clock(now)} Europe/London"
    )
    return embed
