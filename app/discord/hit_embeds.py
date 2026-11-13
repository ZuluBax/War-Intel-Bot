"""War-log embeds for our faction's hits.

Green = a war hit (on the war enemy), orange = a hit on anyone else, gold = a chain
milestone hit (10, 50, 100, 250 ...), which always gets a box of its own. A busy war is ~2 hits a
minute, so hits are batched: consecutive hits of the same kind share one embed
(the coloured box), and several boxes share one Discord message. Time order is
kept — a run of war hits, then a run of outside hits, then war hits again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import discord

from app.database.models import WarHit
from app.utils.formatting import truncate
from app.utils.time import london_clock

WAR_HIT_COLOUR = discord.Colour(0x2ECC71)  # green
OUTSIDE_HIT_COLOUR = discord.Colour(0xE67E22)  # orange
MILESTONE_COLOUR = discord.Colour(0xF1C40F)  # gold

# The chain counts worth shouting about. Torn's ``chain`` on a hit is the chain's
# number *after* that hit (checked on live data: the 100th hit carries the 80-respect
# bonus, the 500th the 320), so equality is the right test. These are Torn's own
# bonus milestones.
CHAIN_MILESTONES = frozenset(
    {10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000}
)

# Discord caps one embed's description at 4096 chars, and a message at 10 embeds
# and 6000 chars summed across all of them. These sit safely under both.
_MAX_DESCRIPTION_CHARS = 3800
MAX_MESSAGE_CHARS = 5500
MAX_EMBEDS_PER_MESSAGE = 10


@dataclass(frozen=True)
class HitMessage:
    """One Discord message's worth of hits, plus which rows it covers."""

    embeds: list[discord.Embed]
    hit_ids: list[int]  # ``war_hits.id`` primary keys


def _name(value: str | None, fallback: str) -> str:
    return discord.utils.escape_markdown(truncate(value, 24)) if value else fallback


def is_chain_milestone(hit: WarHit) -> bool:
    return hit.chain in CHAIN_MILESTONES


def hit_line(hit: WarHit) -> str:
    who = f"**{_name(hit.attacker_name, 'Unknown')}** ➜ **{_name(hit.defender_name, 'Unknown')}**"
    parts = [f"`{london_clock(hit.started_at)}` {who}"]
    if not hit.is_war_hit and hit.defender_faction_name:
        # Outside hits can land on anyone, so say whose faction it was.
        parts[0] += f" _({_name(hit.defender_faction_name, '')})_"
    parts.append(hit.result)
    if hit.respect_gain:
        parts.append(f"+{hit.respect_gain:.2f}")
    if hit.chain:  # None (legacy row) and 0 (not a chain hit) both show nothing
        label = f"🏆 **CHAIN {hit.chain:,}**" if is_chain_milestone(hit) else f"Chain {hit.chain:,}"
        parts.append(label)
    if hit.warlord is not None:  # None: stored before the bonus was kept (backfilled soon)
        # The payout sheet's RW? column: was a ranked war weapon (Warlord bonus) used?
        parts.append("RW: yes" if hit.warlord > 1 else "RW: no")
    return " · ".join(parts)


def _chunk_run(run: Sequence[WarHit]) -> list[list[WarHit]]:
    """Split a same-kind run so each chunk's description fits one embed."""
    chunks: list[list[WarHit]] = []
    current: list[WarHit] = []
    size = 0
    for hit in run:
        line_len = len(hit_line(hit)) + 1  # +1 for the newline
        if current and size + line_len > _MAX_DESCRIPTION_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(hit)
        size += line_len
    if current:
        chunks.append(current)
    return chunks


def _embed_for(chunk: list[WarHit]) -> discord.Embed:
    is_war = chunk[0].is_war_hit
    if is_chain_milestone(chunk[0]):
        # A milestone always sits alone in its chunk (see build_hit_messages).
        kind = "war hit" if is_war else "outside hit"
        title = f"🏆 CHAIN {chunk[0].chain:,} · {kind}"
        colour = MILESTONE_COLOUR
    else:
        title = f"🟢 War hits · {len(chunk)}" if is_war else f"🟠 Outside hits · {len(chunk)}"
        colour = WAR_HIT_COLOUR if is_war else OUTSIDE_HIT_COLOUR
    embed = discord.Embed(
        title=title,
        description="\n".join(hit_line(h) for h in chunk),
        colour=colour,
    )
    # Lines only show the time of day; Discord renders this as the date/time of
    # the batch, which keeps a multi-day backfill readable.
    embed.timestamp = chunk[0].started_at
    return embed


def build_hit_messages(hits: Sequence[WarHit]) -> list[HitMessage]:
    """Turn hits into ready-to-send messages, oldest first, within Discord's limits."""
    ordered = sorted(hits, key=lambda h: (h.started_at, h.id))

    runs: list[list[WarHit]] = []
    for hit in ordered:
        # A milestone hit gets its own gold box, so it never joins (or is joined by) a run.
        joins_previous_run = (
            bool(runs)
            and runs[-1][0].is_war_hit == hit.is_war_hit
            and not is_chain_milestone(hit)
            and not is_chain_milestone(runs[-1][0])
        )
        if joins_previous_run:
            runs[-1].append(hit)
        else:
            runs.append([hit])

    messages: list[HitMessage] = []
    embeds: list[discord.Embed] = []
    ids: list[int] = []
    chars = 0
    for run in runs:
        for chunk in _chunk_run(run):
            embed = _embed_for(chunk)
            if embeds and (
                len(embeds) >= MAX_EMBEDS_PER_MESSAGE or chars + len(embed) > MAX_MESSAGE_CHARS
            ):
                messages.append(HitMessage(embeds, ids))
                embeds, ids, chars = [], [], 0
            embeds.append(embed)
            ids.extend(h.id for h in chunk)
            chars += len(embed)
    if embeds:
        messages.append(HitMessage(embeds, ids))
    return messages
