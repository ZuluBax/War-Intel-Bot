# Target-priority algorithm (spec §17, Phase 6)

`app/war/targeting.py` implements `rank()`, a pure function with no I/O — it
takes a list of `TargetInput` and returns a list of `TargetScore`, ordered
most-attractive-target first. `app/war/target_service.py`'s `TargetingService`
is the only caller: it turns a war's already-built `Roster` (Phase 3/5) into
`TargetInput` rows, calls `rank()`, and persists the result as a snapshot in
`target_rankings` (replaced whole on every refresh, not appended to).

This is called **TARGET PRIORITY**, never **BEST TARGET** — it is a heuristic
built from estimated data, not a guarantee. Every rendering of it (the
`#target-board` channel, `/targets`) carries that disclaimer.

## How the board shows it

`#target-board` and `/targets` render one live list (`app/discord/target_board.py`),
grouped by what each enemy can do right now (`app/war/live_status.py`):
**Available** (this heuristic orders it, best first) → **Hospital / Jail**
(soonest release first, exact time from Torn) → **Landing in Torn** (soonest
first) → **Abroad** → **Flying out**. The score itself is not shown.

**Landing windows — the bot's own (`~`).** Torn publishes **no arrival time** for a
flight. The bot works one out the way FFScouter's paid Flight Tracker does:
take-off time (the bot watches the enemy every minute from when the war is
*announced*, so it sees nearly every take-off) + that route's flight time for that
plane, as a window from fastest to slowest:
`~ lands in 12 min (12:10-12:21) PI`. Rules live in `app/war/live_status.py`
(`_METHOD_RANGE`), fitted to 3,672 real flights (FFScouter premium history, 8
factions). Fair test on 1,252 flights from two factions not used in the fit (Mayans
MC 50228, Big Wheelers 1251): **99.8% of landings inside the window**, FFScouter's
own windows 99.0%. Windows are ~11 min (PI) / ~12 min (Airline) — same as FFScouter's.

Note: Torn made airliner flights ~5% faster around 20-25 Jun 2026 (FFScouter's own
windows moved from 1.000x to 0.947x of the old standard time then). Only data from
after that change should be used to re-fit.

Torn shows a normal ticket and a business-class ticket (BCT, ~3x faster) both as
"airliner". So the bot **remembers every flight it watched** (`flight_records`
table, written in `app/war/roster.py`) and uses each enemy's last airliner ticket.
With no history yet it shows both: `… (12:40-12:44) BCT or 15:02-15:18 Airline`;
the BCT option drops off once its window passes without a landing. WLT (private
jet) times are Torn's rule, not yet measured — no WLT flights seen so far.

"landing time unknown" = the bot did not see the take-off (already flying at
first look, or the bot was down for more than ~3 minutes).

**FFScouter premium (🎯), optional.** If the server's FFScouter key has premium,
`app/war/flights.py` asks FFScouter only about flights whose take-off the bot did
**not** see, and shows its window with 🎯. Without premium nothing changes (the
tracker switches itself off for an hour at a time). Budget: 1 unit per player,
100/min per FFScouter account; re-asked only when a flight changes or every 5 min.
Measured on the same 470 flights: FFScouter caught 94% (its "BCT" label was wrong
for 24 of 34 flights), so the bot's own window is preferred whenever it has one.

## Inputs

| Field | Meaning | When `None` / unknown |
|---|---|---|
| `estimated_bs` | FFScouter's estimated battle strength | We haven't got an estimate for this player yet |
| `fair_fight` | FFScouter's Fair Fight figure | Same |
| `level` | Torn level | Not expected to be `None` in practice, but handled |
| `is_available` | Currently attackable — Torn status is `OKAY` | Anything else (hospital/jail/travelling/unknown) counts as unavailable |

## Scoring

Each of the three weighted factors is **min-max normalised within the batch
passed to `rank()`** — this ranks "who's the easiest pick on THIS roster",
not against some absolute strength scale, so the same algorithm stays
meaningful whether the enemy faction averages level 20 or level 80.

For each factor, lower raw values score *higher* (weaker enemy = more
attractive target):

```
component(value, known_values):
    if value is None:            return 0.0   # unconfirmed never outranks confirmed
    if all known values equal:   return 0.5   # nothing to differentiate on
    else:                        return 1 - (value - min) / (max - min)
```

The composite score is:

```
score = strength_weight   * component(estimated_bs)
      + fair_fight_weight * component(fair_fight)
      + level_weight      * component(level)
```

### Default weights (`TargetingWeights`)

| Weight | Default | Rationale |
|---|---|---|
| `strength_weight` | 0.60 | The dominant signal — estimated battle strength is the best proxy for "can I win this fight" |
| `fair_fight_weight` | 0.25 | Secondary — reward-to-risk, cheaper to compute confidently than raw strength |
| `level_weight` | 0.15 | Weakest signal — level correlates with strength only loosely, but is always known |

They sum to 1.0 by design (enforced by a test, not the type system — a caller
passing custom weights that don't sum to 1.0 gets a score outside a clean
[0, 1] range, which is a caller error, not a validation Phase 6 enforces).

## Availability is a hard gate, not a weight

Status is deliberately **not** a fourth weighted factor. An enemy you cannot
currently attack (hospital, jail, travelling, or unknown status) is:

1. Always sorted **after** every attackable target, regardless of score.
2. Capped at `LOW` priority, regardless of how weak they'd otherwise score.

Their underlying `score` is still computed and preserved (not zeroed) — once
they become available again, the next refresh naturally re-ranks them on
their merits, and `/targets available_only:false` (the default) still lets
you browse them for planning ahead.

## Priority bands

Fixed thresholds on the composite score (`_bucket()` in `targeting.py`):

| Score | Priority |
|---|---|
| ≥ 0.66 | `HIGH` |
| ≥ 0.33 | `MEDIUM` |
| < 0.33 | `LOW` |

Fixed thresholds were chosen over population-relative bands (e.g. "top
third") because they're independent of roster composition and trivial to
reason about and test in isolation — a given score always means the same
thing, regardless of who else is on the roster that refresh.

## Versioning

`ALGORITHM_VERSION = "v1"` is persisted on every `target_rankings` row so a
future `v2` algorithm can be introduced without corrupting the meaning of
historical snapshots (not that history is kept long — the table is a
snapshot, replaced each refresh — but the version travels with the row for
whenever historical retention is added, e.g. Phase 7/8).
