"""Typed value objects for Torn API responses.

These are the *internal* shapes the rest of the app consumes. The mapping from
Torn's raw JSON to these objects lives in ``faction.py`` / ``warfare.py`` /
``players.py`` and is the only place that needs updating if Torn changes a field
name. Unknown / unavailable fields are represented as ``None`` — never invented
(spec §7, §18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class PlayerState(StrEnum):
    OKAY = "OKAY"
    HOSPITAL = "HOSPITAL"
    JAIL = "JAIL"
    TRAVELING = "TRAVELING"
    UNKNOWN = "UNKNOWN"


# Torn's ``status.state`` strings (as returned by API v2) mapped to our enum.
# Anything not listed becomes UNKNOWN rather than being guessed at (spec §18).
_TORN_STATE_MAP: dict[str, PlayerState] = {
    "okay": PlayerState.OKAY,
    "hospital": PlayerState.HOSPITAL,
    "jail": PlayerState.JAIL,
    "traveling": PlayerState.TRAVELING,
    "travelling": PlayerState.TRAVELING,
    "abroad": PlayerState.TRAVELING,
    "federal": PlayerState.JAIL,
}


def parse_player_state(raw: str | None) -> PlayerState:
    if not raw:
        return PlayerState.UNKNOWN
    return _TORN_STATE_MAP.get(raw.strip().lower(), PlayerState.UNKNOWN)


@dataclass(frozen=True)
class TornStatus:
    state: PlayerState = PlayerState.UNKNOWN
    description: str | None = None
    # Torn's ``details``: the reason behind a hospital/jail stay, e.g. "Overdosed on Xanax"
    # (may hold HTML links for attacker names).
    details: str | None = None
    until: datetime | None = None
    # Torn's ``plane_image_type`` (airliner / light_aircraft / private_jet). Torn only
    # sends it while the player is flying; it is the only hint we get about flight speed.
    plane_type: str | None = None


@dataclass(frozen=True)
class FactionMember:
    player_id: int
    name: str | None = None
    level: int | None = None
    position: str | None = None
    days_in_faction: int | None = None
    last_action: datetime | None = None
    # Torn's own label for how recently they were active: "Online" / "Idle" / "Offline".
    # Not derivable from ``last_action`` (Torn labels some recent actions Idle/Offline), so
    # the label is kept as sent.
    online_status: str | None = None
    status: TornStatus = field(default_factory=TornStatus)


@dataclass(frozen=True)
class FactionSummary:
    faction_id: int
    name: str | None = None
    members: tuple[FactionMember, ...] = ()


@dataclass(frozen=True)
class AttackRecord:
    """One attack from a faction's attack feed (``/faction/{id}/attacks``).

    ``attacker_*`` are ``None`` when Torn hides the attacker — a *stealthed* hit
    made **on** our faction. Our own stealthed hits still carry the attacker.
    ``is_ranked_war`` is Torn's own flag: the hit counted towards a ranked war.
    """

    attack_id: int
    started: datetime
    attacker_id: int | None
    attacker_name: str | None
    attacker_faction_id: int | None
    defender_id: int | None
    defender_name: str | None
    defender_faction_id: int | None
    defender_faction_name: str | None
    result: str
    respect_gain: float | None
    is_ranked_war: bool
    chain: int | None = None  # our chain's number at this hit (0 / None: not a chain hit)
    # Respect modifier from a Warlord-bonus weapon (1.0 = none, 1.15 = +15%). None when
    # Torn sent no modifiers for the attack.
    warlord: float | None = None


@dataclass(frozen=True)
class RankedWarFaction:
    faction_id: int
    name: str | None = None
    score: int | None = None
    chain: int | None = None


@dataclass(frozen=True)
class RankedWar:
    """A single ranked war as reported by Torn.

    ``war_id`` is Torn's own identifier (the object key in the ``rankedwars``
    response). ``winner`` / ``end`` are ``None`` while the war is live.
    """

    war_id: int
    start: datetime | None
    end: datetime | None
    target_score: int | None
    factions: tuple[RankedWarFaction, ...]
    winner_faction_id: int | None = None

    def opponent_of(self, our_faction_id: int) -> RankedWarFaction | None:
        for f in self.factions:
            if f.faction_id != our_faction_id:
                return f
        return None

    def includes(self, faction_id: int) -> bool:
        return any(f.faction_id == faction_id for f in self.factions)
