# Verified API contracts

Per the spec's rule (§42): **verify the current official API documentation before
writing API-specific code.** This file records what was actually checked and
when. Re-verify against the live sources before relying on anything here.

Last verified: **2026-09-10** (Torn section re-verified against the live API
with a real faction key — see "Live probe results" below).

---

## Torn API (v2)

Sources: <https://www.torn.com/api.html>, the v2 playground
(<https://api-playground.torn.com/>), and a working v2 client in a sibling
project.

| Fact | Value | Confidence |
|---|---|---|
| Base URL | `https://api.torn.com/v2` | Verified |
| Auth header | `Authorization: ApiKey <key>` | Verified |
| Rate limit | 100 requests / minute **per user** (all their keys combined) | Verified |
| Error 5 | rate limit exceeded — back off | Verified |
| Error 8 | temporary IP ban from abuse — back off hard | Verified |
| Error 16 | key access level too low for the selection | Verified |
| Service cache | up to ~30s; bypass with a unique `timestamp` param (not needed here) | Verified |
| Faction selections available | `basic`, `members`, `wars`, `rankedwars`, `rankedwarreport`, `warfare` | Verified (list); field shapes **pending** |
| `rankedwars` (global) shape | `{ "<warId>": { "factions": { "<facId>": {name, score, chain} }, "war": {start, end, target, winner?} } }` — epoch seconds | Verified shape; **which key access level is needed to see your own pending war is PENDING** |
| Enemy roster source | `/v2/faction/{id}/members` — id, name, level, status{}, last_action, days_in_faction, position | Shape **pending live check** |
| Player status source | `/v2/user/{id}` `profile`/`status` — hospital/jail/travel + `until` | Shape **pending live check** |
| Bulk user endpoint | **does not exist** — status refresh is one request per player | Verified (sibling project) |

### Live probe results (2026-09-10, our faction)

The configured key (owner is a faction member) was probed with
`scripts/probe_torn.py`. Confirmed:

| Endpoint | Result | Notes |
|---|---|---|
| `GET /v2/user?selections=profile` | OK | `{"profile": {id, name, level, faction_id, status{}, last_action{}, life{}, …}}` — faction id is the flat int `profile.faction_id`, **not** a nested object. |
| `GET /v2/user/{id}?selections=profile` | OK for arbitrary players | Used for enemy player level + status. |
| `GET /v2/faction/{id}/basic` | OK for **our own and an arbitrary enemy** faction | `{"basic": {id, name, tag, leader_id, respect, days_old, capacity, members, rank{level,name,division,position,wins}, best_chain}}` |
| `GET /v2/faction/{id}/members` | OK for **our own and an arbitrary enemy** faction | `{"members": [ {id, name, level, days_in_faction, last_action{status,timestamp,relative}, status{description,details,state,color,until}, position, revive_setting, is_revivable, is_on_wall, is_in_oc, has_early_discharge} ]}` |
| `GET /v2/faction/{id}/wars` | OK | `{"pacts": [...], "wars": {"ranked": null \| <object>, "raids": [], "territory": []}}` — `wars.ranked` is `null` when there is no current ranked war. **This is the "is there a war right now" check.** |
| `GET /v2/faction/{id}/rankedwars` | OK for our own and an arbitrary faction | `{"rankedwars": [ {id, start, end, target, winner, factions: [{id, name, score, chain}]} ], "_metadata": {...}}` — newest first; includes the current war and full history. `end`/`winner` are populated only once finished. Epoch seconds. **Primary source for both detection detail and war history.** |
| `GET /v2/torn?selections=rankedwars` | **FAIL — Torn error 22** ("only available in API v1") | Not needed. We never query the global list; we work from our own / the enemy's faction-scoped endpoints. |

| `GET /v2/faction/{id}/attacks?limit=&sort=&from=&to=` | OK for **our own** faction (2026-09-19, live war) | `{"attacks": [ {id, started, ended, attacker{id,name,level,faction{id,name}} \| null, defender{…}, result, respect_gain, respect_loss, chain, is_stealthed, is_interrupted, is_ranked_war, modifiers{…}} ], "_metadata": {links}}`. Holds hits **by and on** the faction. `limit` max 100; `from` is inclusive (and appears to match on the attack's *end* time — an attack that started 3s before `from` still came back). `attacker` is `null` on a stealthed hit **on** us; our own stealthed hits still show the attacker. `chain` is our chain's number at the hit (0 on Lost hits and assists). `modifiers.warlord` is the respect multiplier of a **Warlord**-bonus weapon (1.0 = none; seen 1.15 / 1.17 / 1.18) and matches `finishing_hit_effects: [{name: warlord}]`; it is present on ~20% of our hits but only for 6 of 37 players over 5 wars, i.e. it behaves like a weapon someone owns. **Torn's attack feeds name no weapon at all** (checked on 200 real attacks in `attacks` and `attacksfull`), so this bonus is the only trace of a ranked war weapon. `is_ranked_war` is **only true for hits that scored** — losses / interrupts / assists on the war enemy come back `false` (37 of 41 unflagged hits in the live sample), so "hit the enemy faction" must be checked separately. `result` seen: Attacked, Hospitalized, Mugged, Lost, Assist, Interrupted. Works with a **Limited Access** key (`/key/info` -> level 3, faction access). |

**Key access level:** this key can read another faction's `basic`,
`members` and `rankedwars`, plus any player's `profile`. That corresponds to a
**Limited Access** key (or a Custom key with the faction + user selections
ticked) held by a faction member. A Public-only key was **not** tested; treat
Limited as the documented minimum until proven otherwise. → README §3.

### Still to confirm (needs a live ranked war)

1. The exact shape of `wars.ranked` when it is **non-null** (an active/pending
   war). The finished-war list shape from `/rankedwars` is confirmed; the live
   in-progress object may carry extra fields (per-faction live score, `war`
   sub-object, target). Re-probe during the next war and update the mapper in
   `app/torn/warfare.py` if needed.
2. Whether a *pending* war (matchmade, `start` in the future) appears in
   `wars.ranked` / `rankedwars` before it starts — decides detection lead time.
3. Enemy player `status.state` spellings beyond `"Okay"` (i.e. exact strings for
   hospital / jail / travelling / federal). Seen so far: `state: "Okay"`,
   `description: "Okay"`, `color: "green"`, `until: null`.

---

## FFScouter API (v1)

Source: <https://ffscouter.com/api-docs>.

| Fact | Value | Confidence |
|---|---|---|
| Base URL | `https://ffscouter.com/api/v1` | Verified |
| Auth | `key` query parameter, 16-char alphanumeric, registered via `POST /register` | Verified |
| Primary endpoint | `GET /get-stats?key=<key>&targets=<comma-separated ids>` | Verified |
| Batch size | up to **205** target IDs per request (bot batches at 200) | Verified |
| Rate limit | **20 requests / minute per IP** | Verified |
| Response | JSON **array**, one object per requested target | Verified |
| Row fields | `player_id`, `fair_fight`, `bs_estimate`, `bs_estimate_human`, `bss_public`, `last_updated` (unix), `source` (`bss`/`premium`/`spies`), `premium_insights_available` | Verified |
| Error codes | numeric 1–79 with HTTP status; 6 = bad key (401), 19 = premium required (403), 20 = rate limit (429) | Verified |
| Premium-only | `/player-flights`, `/activity/*`, hit-calling, stat `distribution` | Verified |
| Live error shape | `{"code": 19, "error": "Active premium subscription required ..."}` — code at the **top level**, not inside `error` as the docs show. Client accepts both. | Verified live 2026-09-22 |
| Flights (premium) | `GET /player-flights/batch?key&targets` (max 100). Reply `{"flights": [{"player_id", "current": null \| {takeoff_time, status_description, earliest_arrival_time, latest_arrival_time, travel_method, book_likely_being_used}}]}`; times unix seconds; `travel_method` ∈ PI/Airline/WLT/BCT/Unknown. Budget 100 units/min **per FFScouter account**, 1 unit per target. Used by `app/war/flights.py`. | Shape from docs; premium path not yet exercised live (no premium key) |

### Open items to close in Phase 3

1. Exact error body shape (`{"error": {"code", "message"}}` assumed — confirm
   live and adjust `app/ffscouter/client.py::_get_stats_batch` if different).
2. What `bs_estimate` is when FFScouter has no data — `null`, `0`, or field
   absent. The client treats non-numeric as "no estimate".
3. Whether `last_updated` can be `0`/absent and how old an estimate FFScouter
   will still return (feeds the confidence rubric, spec §13).
