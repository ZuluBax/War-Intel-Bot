"""Real-shaped Torn API v2 response fixtures.

Captured from the live API on 2026-09-10 (faction 53228) via
``scripts/probe_torn.py`` and trimmed. Keep these in sync with
``docs/api-contracts.md`` if Torn changes a field.
"""

from __future__ import annotations

from typing import Any

FACTION_BASIC: dict[str, Any] = {
    "basic": {
        "id": 53228,
        "name": "Null & Void",
        "tag": "N&V",
        "leader_id": 3954550,
        "co_leader_id": 0,
        "respect": 123456,
        "days_old": 400,
        "capacity": 40,
        "members": 3,
        "rank": {"level": 5, "name": "Bronze", "division": 2, "position": 1, "wins": 12},
        "best_chain": 250,
    }
}

FACTION_MEMBERS: dict[str, Any] = {
    "members": [
        {
            "id": 1785078,
            "name": "Katz047",
            "level": 20,
            "days_in_faction": 18,
            "last_action": {"status": "Offline", "timestamp": 1789060391, "relative": "33 minutes ago"},
            "status": {
                "description": "Okay",
                "details": None,
                "state": "Okay",
                "color": "green",
                "until": None,
            },
            "position": "Sinner",
            "is_revivable": True,
        },
        {
            "id": 2185019,
            "name": "Xapd0",
            "level": 50,
            "days_in_faction": 243,
            "last_action": {"status": "Online", "timestamp": 1789056960, "relative": "1 hour ago"},
            "status": {
                "description": "In hospital for 5 mins",
                "details": "Hospitalized",
                "state": "Hospital",
                "color": "red",
                "until": 1789061000,
            },
            "position": "Sinner",
            "is_revivable": True,
        },
        {
            "id": 2250132,
            "name": "nicekhid",
            "level": 40,
            "days_in_faction": 273,
            "last_action": {"status": "Offline", "timestamp": 1789037751, "relative": "6 hours ago"},
            "status": {
                "description": "Travelling to Mexico",
                "details": None,
                "state": "Traveling",
                "color": "blue",
                "until": None,
            },
            "position": "Sinner",
            "is_revivable": False,
        },
    ]
}

# /faction/{id}/rankedwars — newest first; all finished (end + winner set).
FACTION_RANKEDWARS_FINISHED: dict[str, Any] = {
    "rankedwars": [
        {
            "id": 48328,
            "start": 1787860800,
            "end": 1788048496,
            "target": 4473,
            "winner": 49384,
            "factions": [
                {"id": 49384, "name": "7DS*: Wrath", "score": 10228, "chain": 0},
                {"id": 53228, "name": "Null & Void", "score": 5755, "chain": 0},
            ],
        },
        {
            "id": 47882,
            "start": 1787256000,
            "end": 1787331623,
            "target": 5200,
            "winner": 53228,
            "factions": [
                {"id": 51166, "name": "Bigger on the Inside", "score": 2633, "chain": 0},
                {"id": 53228, "name": "Null & Void", "score": 7836, "chain": 0},
            ],
        },
    ],
    "_metadata": {"links": {}},
}

# A hypothetical *active* war: newest entry has end=None, winner=None.
FACTION_RANKEDWARS_ACTIVE: dict[str, Any] = {
    "rankedwars": [
        {
            "id": 49999,
            "start": 1789050000,
            "end": None,
            "target": 5000,
            "winner": None,
            "factions": [
                {"id": 53228, "name": "Null & Void", "score": 1200, "chain": 3},
                {"id": 60001, "name": "Rival Crew", "score": 900, "chain": 0},
            ],
        },
        *FACTION_RANKEDWARS_FINISHED["rankedwars"],
    ],
    "_metadata": {"links": {}},
}

FACTION_WARS_NO_RANKED: dict[str, Any] = {
    "pacts": [],
    "wars": {"ranked": None, "raids": [], "territory": []},
}

USER_PROFILE: dict[str, Any] = {
    "profile": {
        "id": 60123,
        "name": "EnemyGuy",
        "level": 44,
        "faction_id": 60001,
        "status": {
            "description": "Okay",
            "details": None,
            "state": "Okay",
            "color": "green",
            "until": None,
        },
        "last_action": {"status": "Online", "timestamp": 1789061000, "relative": "1 minute ago"},
    }
}


def attack(
    attack_id: int,
    started: int,
    *,
    attacker: tuple[int, str, int, str] | None = (2086615, "The_eVagabond", 53228, "Null & Void"),
    defender: tuple[int, str, int, str] = (3983945, "NoZuaL", 60001, "Rival Crew"),
    result: str = "Attacked",
    respect_gain: float = 4.68,
    is_ranked_war: bool = True,
    chain: int = 182,
    warlord: float = 1.0,
) -> dict[str, Any]:
    """One row of ``/faction/{id}/attacks``, shaped like the live API (captured
    2026-09-19). ``attacker=None`` mimics a stealthed hit made *on* our faction,
    where Torn hides the attacker entirely."""

    def side(who: tuple[int, str, int, str] | None) -> dict[str, Any] | None:
        if who is None:
            return None
        pid, name, fid, fname = who
        return {"id": pid, "name": name, "level": 50, "faction": {"id": fid, "name": fname}}

    return {
        "id": attack_id,
        "code": f"code{attack_id}",
        "started": started,
        "ended": started + 4,
        "attacker": side(attacker),
        "defender": side(defender),
        "result": result,
        "respect_gain": respect_gain,
        "respect_loss": 1.17,
        "chain": chain,
        "is_interrupted": False,
        "is_stealthed": attacker is None,
        "is_raid": False,
        "is_ranked_war": is_ranked_war,
        "is_territory_war": False,
        "territory_war_id": None,
        "modifiers": {"fair_fight": 1.44, "war": 2 if is_ranked_war else 1, "warlord": warlord},
        "finishing_hit_effects": [],
    }


def attacks_page(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"attacks": list(rows), "_metadata": {"links": {"prev": None, "next": None}}}
