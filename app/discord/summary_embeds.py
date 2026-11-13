"""The end-of-war scoreboard for ``#war-log``: one row per player who hit, then the
enemy overdoses seen during the war."""

from __future__ import annotations

from collections.abc import Sequence

import discord

from app.database.repositories import OverdoseView
from app.discord.hit_embeds import MAX_EMBEDS_PER_MESSAGE, MAX_MESSAGE_CHARS
from app.utils.formatting import truncate
from app.war.hit_summary import PlayerHitSummary

# Discord caps one embed's description at 4096 chars (same headroom as
# app.discord.hit_embeds._MAX_DESCRIPTION_CHARS). Rows here are fixed width and much
# narrower than that table's, so pages are sized against this real budget rather than
# a borrowed row count — a fixed 35-per-page guess used to split a 36-player war into
# two embeds well before Discord's actual limit was anywhere close.
_MAX_DESCRIPTION_CHARS = 3800
_NAME_WIDTH = 16  # Torn's name limit

_RESULT_COLOUR: dict[str, discord.Colour] = {
    "WIN": discord.Colour(0x2ECC71),
    "LOSS": discord.Colour(0xE74C3C),
}
_DEFAULT_COLOUR = discord.Colour(0x95A5A6)

_HEADER = f"{'PLAYER':<{_NAME_WIDTH}}  {'HITS':>4}  {'W':>3}  {'D':>3}  {'L':>3}  {'A':>3}"


def _row(p: PlayerHitSummary) -> str:
    name = truncate(p.name, _NAME_WIDTH)
    return (
        f"{name:<{_NAME_WIDTH}}  {p.hits:>4}  {p.won:>3}  {p.draw:>3}  {p.lost:>3}  {p.assist:>3}"
    )


def _table(rows: Sequence[str]) -> str:
    return "```\n" + "\n".join([_HEADER, "-" * len(_HEADER), *rows]) + "\n```"


def _paginate(rows: Sequence[str], *, first_page_prefix_chars: int) -> list[list[str]]:
    """Group table rows into pages that each fit under Discord's description cap.

    Accumulates against the real character budget (like hit_embeds._chunk_run) instead
    of a fixed row count, so a page only splits when it would actually overflow.
    """
    pages: list[list[str]] = []
    current: list[str] = []
    for row in rows:
        prefix = first_page_prefix_chars if not pages else 0
        if current and len(_table([*current, row])) + prefix > _MAX_DESCRIPTION_CHARS:
            pages.append(current)
            current = []
        current.append(row)
    if current:
        pages.append(current)
    return pages


def war_summary_embeds(
    *,
    our_faction_name: str | None,
    enemy_faction_name: str | None,
    result: str | None,
    players: Sequence[PlayerHitSummary],
) -> list[discord.Embed]:
    title = f"🏁 War summary · {our_faction_name or 'Us'} vs {enemy_faction_name or 'Unknown'}"
    colour = _RESULT_COLOUR.get(result or "", _DEFAULT_COLOUR)

    if not players:
        return [
            discord.Embed(
                title=title, description="No hits were recorded for this war.", colour=colour
            )
        ]

    totals = (
        f"**{len(players)}** players · **{sum(p.hits for p in players)}** hits · "
        f"won **{sum(p.won for p in players)}** · draw **{sum(p.draw for p in players)}** · "
        f"lost **{sum(p.lost for p in players)}** · assist **{sum(p.assist for p in players)}**"
    )
    pages = _paginate([_row(p) for p in players], first_page_prefix_chars=len(totals) + 1)

    embeds: list[discord.Embed] = []
    for n, page in enumerate(pages, start=1):
        table = _table(page)
        embed = discord.Embed(
            title=title if n == 1 else f"{title} ({n}/{len(pages)})",
            description=f"{totals}\n{table}" if n == 1 else table,
            colour=colour,
        )
        if n == len(pages):
            embed.set_footer(
                text="W won · D draw · L lost · A assist. HITS counts every attack "
                "(interrupted ones too). War hits only."
            )
        embeds.append(embed)
    return embeds


def overdose_summary_embed(overdoses: Sequence[OverdoseView]) -> discord.Embed | None:
    """Every enemy overdose the bot saw this war (app.war.overdose), oldest first.
    None when there were none, so the summary isn't padded with an empty box."""
    if not overdoses:
        return None
    rows: list[str] = []
    size = 0
    ordered = sorted(overdoses, key=lambda od: od.first_seen_at)
    for i, od in enumerate(ordered):
        name = discord.utils.escape_markdown(truncate(od.name or str(od.torn_player_id), 20))
        seen = int(od.first_seen_at.timestamp())
        row = f"🤢 {name} — {truncate(od.drug, 16)} · seen <t:{seen}:f>"
        if size + len(row) + 1 > _MAX_DESCRIPTION_CHARS - 40:  # room for the "more" line
            rows.append(f"…and {len(ordered) - i} more")
            break
        rows.append(row)
        size += len(row) + 1
    return discord.Embed(
        title=f"🤢 Enemy overdoses this war ({len(overdoses)})",
        description="\n".join(rows),
        colour=_DEFAULT_COLOUR,
    )


def war_summary_messages(
    *,
    our_faction_name: str | None,
    enemy_faction_name: str | None,
    result: str | None,
    players: Sequence[PlayerHitSummary],
    overdoses: Sequence[OverdoseView] = (),
) -> list[list[discord.Embed]]:
    """The scoreboard (plus the overdose list, if any) packed into as few messages as
    Discord's limits allow."""
    messages: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    chars = 0
    embeds = war_summary_embeds(
        our_faction_name=our_faction_name,
        enemy_faction_name=enemy_faction_name,
        result=result,
        players=players,
    )
    od_embed = overdose_summary_embed(overdoses)
    if od_embed is not None:
        embeds.append(od_embed)
    for embed in embeds:
        if current and (
            len(current) >= MAX_EMBEDS_PER_MESSAGE or chars + len(embed) > MAX_MESSAGE_CHARS
        ):
            messages.append(current)
            current, chars = [], 0
        current.append(embed)
        chars += len(embed)
    if current:
        messages.append(current)
    return messages
