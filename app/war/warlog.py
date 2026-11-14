"""Meaningful war-log event construction (spec §32).

Deliberately pure — no I/O, no DB session — so the wording is trivially unit
tested and callers stay in control of when a row is actually persisted and
posted. Not an API-request audit log: only events a human following the war
would care about.
"""

from __future__ import annotations

from datetime import datetime

from app.database.models import WarLogEvent

WAR_SUMMARY_KIND = "WAR_SUMMARY"

_RESULT_LABEL = {"WIN": "won", "LOSS": "lost", "DRAW": "drawn"}


def war_registered_event(war_id: int, *, enemy_faction_name: str | None) -> WarLogEvent:
    name = enemy_faction_name or "an unknown faction"
    return WarLogEvent(
        war_id=war_id, kind="WAR_REGISTERED", message=f"Ranked war detected against {name}."
    )


def state_change_event(war_id: int, *, was: str, now: str) -> WarLogEvent:
    return WarLogEvent(
        war_id=war_id,
        kind="STATE_CHANGE",
        message=f"War state changed: {was.replace('_', ' ')} → {now.replace('_', ' ')}.",
    )


def war_finished_event(war_id: int, *, result: str | None) -> WarLogEvent:
    label = _RESULT_LABEL.get(result or "", "ended")
    return WarLogEvent(war_id=war_id, kind="WAR_FINISHED", message=f"The war has {label}.")


def war_summary_event(war_id: int) -> WarLogEvent:
    """Marker for the end-of-war scoreboard. The message is a placeholder: the
    board renders the real table from ``war_hits`` at post time, so it includes
    the final sweep of hits. Reusing a log event gives once-only posting for free."""
    return WarLogEvent(war_id=war_id, kind=WAR_SUMMARY_KIND, message="War summary")


def roster_join_event(war_id: int, *, player_id: int, name: str | None) -> WarLogEvent:
    who = name or f"Player {player_id}"
    return WarLogEvent(
        war_id=war_id, kind="ROSTER_JOIN", message=f"{who} joined the enemy faction."
    )


def roster_left_event(war_id: int, *, player_id: int, name: str | None) -> WarLogEvent:
    who = name or f"Player {player_id}"
    return WarLogEvent(
        war_id=war_id, kind="ROSTER_LEFT", message=f"{who} left the enemy faction."
    )


HIT_FEED_BLOCKED_KIND = "HIT_FEED_BLOCKED"


def hit_feed_blocked_event(war_id: int, *, access_level_too_low: bool) -> WarLogEvent:
    """Torn refuses the saved key access to the faction's attacks, so no hits will
    appear. Without this the only trace is a server log line nobody in Discord sees."""
    if access_level_too_low:
        cause = (
            "the saved Torn API key's access level is too low to read the faction's "
            "attacks. Run `/setup torn-key` with a Limited (or higher) access key"
        )
    else:
        cause = (
            "Torn won't let the saved API key read the faction's attacks. A faction "
            "leader needs to give the key owner's position **Faction API Access** "
            "(Faction → Controls → Positions), or run `/setup torn-key` with a key "
            "from a member who has it"
        )
    return WarLogEvent(
        war_id=war_id,
        kind=HIT_FEED_BLOCKED_KIND,
        message=(
            f"Hits can't be shown: {cause}. Hits from this war are filled in once it "
            "works, as long as the war is still running."
        ),
    )


OVERDOSE_KIND = "OVERDOSE"


def overdose_event(
    war_id: int, *, player_id: int, name: str | None, drug: str, hospital_until: datetime | None
) -> WarLogEvent:
    who = name or f"Player {player_id}"
    message = f"{who} overdosed on {drug}"
    if hospital_until is not None:
        # Discord timestamps: each reader sees their own local time and a live countdown.
        ts = int(hospital_until.timestamp())
        message += f" — in hospital until <t:{ts}:f> (<t:{ts}:R>)"
    return WarLogEvent(war_id=war_id, kind=OVERDOSE_KIND, message=f"{message}.")
