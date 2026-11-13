"""Diagnostic: check what the configured Torn API key can actually read.

Run after setting at least TORN_API_KEY in .env (TORN_FACTION_ID is optional —
this script discovers it from the key owner's profile if it is not set):

    uv run python -m scripts.probe_torn

It makes a handful of read-only requests, reports which endpoints the key can
access, and prints a short summary + the discovered faction id. It never prints
the API key.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.config.settings import get_settings
from app.torn.client import TornClient
from app.torn.errors import TornAPIError


def _shape(body: Any) -> str:
    if isinstance(body, dict):
        keys = list(body)[:10]
        return "{" + ", ".join(map(str, keys)) + ("…" if len(body) > 10 else "") + "}"
    if isinstance(body, list):
        return f"[{len(body)} items] " + (_shape(body[0]) if body else "")
    return type(body).__name__


async def main() -> int:
    settings = get_settings()
    if settings.torn_api_key is None:
        print("TORN_API_KEY is not set in .env — nothing to probe.", file=sys.stderr)
        return 2

    async with TornClient.from_settings(settings) as client:
        # 1. Does the key work at all, and who owns it / which faction?
        try:
            me = await client.get("/user?selections=profile")
        except TornAPIError as exc:
            print(f"  FAIL  key rejected -> {exc}")
            return 1

        profile = me.get("profile", me)
        owner = profile.get("name", "?")
        owner_id = profile.get("id", "?")
        discovered_fid = profile.get("faction_id") or None
        print(f"  OK    key works - owner {owner} [{owner_id}], level {profile.get('level')}")
        print(f"        profile.faction_id = {discovered_fid}")

        fid = settings.torn_faction_id or discovered_fid
        if fid is None:
            print("\nCould not determine a faction id (key owner has no faction?).")
            return 1
        print(f"\nUsing faction id: {fid}"
              + ("  (from .env)" if settings.torn_faction_id else "  (discovered from key)"))
        print()

        probes: list[tuple[str, str]] = [
            ("our faction basic", f"/faction/{fid}/basic"),
            ("our faction members", f"/faction/{fid}/members"),
            ("our faction wars", f"/faction/{fid}/wars"),
            ("our faction rankedwars", f"/faction/{fid}/rankedwars"),
            ("global torn rankedwars", "/torn?selections=rankedwars"),
            ("other faction members (id 1)", "/faction/1/members"),
            ("a player profile (id 1)", "/user/1?selections=profile"),
        ]

        results: dict[str, str] = {}
        for label, path in probes:
            try:
                body = await client.get(path)
                results[label] = "OK"
                print(f"  OK    {label:32s} -> {_shape(body)}")
                if "war" in label:
                    print("        " + json.dumps(body, default=str)[:700])
            except TornAPIError as exc:
                results[label] = f"FAIL: {exc}"
                print(f"  FAIL  {label:32s} -> {exc}")

    print("\nSummary:")
    for label, outcome in results.items():
        print(f"  - {label}: {outcome}")
    if not settings.torn_faction_id and discovered_fid:
        print(f"\n>> Set TORN_FACTION_ID={discovered_fid} in your .env file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
