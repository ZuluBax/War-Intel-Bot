"""Spotting enemy overdoses from Torn's hospital reason.

An overdose that hospitalises (Xanax: ~80-83h) shows on the player's public status as
``details`` = "Overdosed on Xanax" for the whole stay. The stay is far longer than the
2-3 days between a war being announced and starting, and the bot looks at the enemy every
minute from the announcement, so every such overdose since then is seen — with no extra
API calls. Overdoses that don't hospitalise (Ecstasy: energy + happiness only) never show
on the status and are not caught (owner's call, 9 Oct 2026).

Pure functions — no I/O.
"""

from __future__ import annotations

import re

# "Overdosed on Xanax" (owner-confirmed wording). Matched loosely — case, "some", and any
# HTML Torn wraps around the drug name — so other drugs and small wording changes still match.
_OVERDOSE = re.compile(r"overdosed\s+on\s+(?:some\s+)?(?P<drug>[A-Za-z][A-Za-z \-]{0,30})", re.I)
_TAG = re.compile(r"<[^>]+>")


def overdose_drug(details: str | None) -> str | None:
    """The drug from a hospital reason like "Overdosed on Xanax", else None."""
    if not details:
        return None
    match = _OVERDOSE.search(_TAG.sub("", details))
    if match is None:
        return None
    return match.group("drug").strip() or None
