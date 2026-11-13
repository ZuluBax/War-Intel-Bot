# Architecture

## Multi-tenant

One process serves many factions' Discord servers. There is no fixed guild —
the bot is invited via its OAuth2 link and each server configures itself with
`/setup` (its Torn faction ID + its own Torn API key, stored **encrypted** in
`guild_configs` via `app/services/crypto.py`). `MultiGuildWarRunner` runs one
detection pass per configured server on each scheduler tick, each with its own
`TornClient` (Torn's rate limit is per key); FFScouter's per-IP limit is a
single shared limiter. `wars`, and all reads behind `/war` etc., are scoped by
`guild_id`.

## Shape

A single long-lived process. One `asyncio` event loop runs three things:

1. the **Discord gateway client** (`discord.py`),
2. the **APScheduler** background scheduler (war-state poll + refresh jobs),
3. ad-hoc work triggered by slash commands.

```
                    ┌──────────────────────────────┐
   Discord  ◀──────▶│      TornWarIntelBot          │
   gateway          │  (app/discord/bot.py)         │
                    └──────────────┬───────────────┘
                                   │ composes
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
     ┌────────────────┐  ┌──────────────────┐  ┌───────────────────┐
     │  WarManager    │  │  Scheduler       │  │  Slash commands   │
     │ app/war/*      │  │ app/services/*   │  │ app/discord/*     │
     └───────┬────────┘  └──────────────────┘  └─────────┬─────────┘
             │                                            │
   ┌─────────┼───────────────┐                            │
   ▼         ▼               ▼                            ▼
┌───────┐ ┌───────────┐ ┌──────────────┐          ┌──────────────┐
│TornCl.│ │FFScouterCl│ │  Database    │◀─────────│ repositories │
│v2 API │ │ v1 API    │ │ SQLAlchemy   │          │ (queries)    │
└───────┘ └───────────┘ └──────────────┘          └──────────────┘
```

## Rules that keep it decoupled

- **One client per upstream.** Every Torn call goes through `TornClient`; every
  FFScouter call through `FFScouterClient`. Neither is imported outside its
  package's service layer. Each owns its own rate limiter.
- **Repositories own queries.** No `select()` outside `app/database/repositories.py`.
- **`WarManager` composes, it doesn't implement.** It has no HTTP or Discord
  code — it calls services. This is what lets FFScouter be swapped for an
  in-house estimator later (spec §23) without touching war orchestration.
- **Config enters once.** `app/config/settings.py` is the only reader of the
  environment. Everything else takes a `Settings` object.
- **Secrets are `SecretStr`.** Unwrapped only at the point of use; a logging
  redaction processor is the second line of defence.
- **Idempotency via the DB.** `wars.torn_war_id` and
  `discord_channels (war_id, purpose)` are unique. Seeing a war twice updates;
  it never duplicates (spec §30).
- **Estimates are append-only.** `stat_estimates` gains a row per observation;
  trend/confidence are derived. A failed refresh never deletes the last good row.
- **Confirmed ≠ estimated.** Manual spy reports go in `confirmed_stats`, a
  separate table, never mixed with `stat_estimates` (spec §24).
- **Time is UTC internally**, converted to `Europe/London` only for display
  (`app/utils/time.py`), so DST is never hard-coded.

## Why these technology choices

| Choice | Reason |
|---|---|
| `discord.py` 2.x, slash commands, no privileged intents | All data comes from Torn, not from reading Discord messages. Minimal attack surface. |
| `httpx` async | One async stack end-to-end; no thread pool for HTTP. |
| SQLAlchemy 2.x async + SQLite(dev)/Postgres(prod) | Same ORM both environments; SQLite keeps local dev zero-setup. |
| APScheduler | Simple in-process cron/interval jobs; no external broker. |
| `structlog` | Structured JSON logs in prod, readable console in dev, with secret redaction. |
| `uv` | Fast, reproducible installs; manages the Python 3.12 toolchain itself. |
| No Redis | Single instance; an in-process `TTLCache` is enough (spec §35). Revisit only if scaled out. |
