# Development roadmap

Incremental delivery. The bot must be runnable after every phase (spec §44).
Status legend: ✅ done · 🔨 in progress · ⬜ not started.

| Phase | Scope | Deliverable | Status |
|---|---|---|---|
| **1 — Foundation** | repo, config, logging, DB layer + schema, rate limiter, cache, scheduler, Torn client, FFScouter client, Discord bot skeleton + slash-command surface, tests, Docker, docs | Bot connects to Discord and registers commands | ✅ |
| **2 — Torn integration** | `FactionService` (basic + members, ours & enemy), `PlayerService` (profile + status), `WarfareService` (ranked wars), `WarDetector`, `WarManager.tick` reconciling the `wars` table idempotently, scheduler job at `WAR_CHECK_INTERVAL_SECONDS` | ✅ verified live: detects faction 53228's latest war, records it (WIN/LOSS), no duplicate rows on re-run | ✅ |
| **3 — FFScouter** | `EnemyRosterService` (Torn members + FFScouter estimates → Player/WarPlayer/StatEstimate/PlayerStatusHistory rows, append-only), `IntelligenceService` (trend + confidence from history), `/enemy` + `/stats` commands, roster build hooked into `WarManager` on war registration, FFScouter-down fallback to last known estimate | `/stats <player>` returns a real estimate + trend; verified live: 58 real enemy members fetched, persisted, ranked | ✅ (live FFScouter call pending the user's key) |
| **4 — Discord war creation** | `GuildGateway` abstraction (real + fake), `WarChannelManager` (idempotent category + 5 channels, recreates a deleted channel), `WarBoard` (post-once then edit-in-place, reposts if the message was deleted), `war_info_embed` + `roster_embed`, `DiscordWarPublisher` wired into `WarManager`, `/war` `/war-status` `/enemies` read from the DB | logic verified with a fake gateway (create-once, idempotent, edit-in-place); live Discord run pending the bot token | ✅ (live pending token) |
| **4.5 — Multi-server** | invite-based (no fixed guild); per-server config in `guild_configs` with **encrypted** Torn/FFScouter keys (`SecretCipher` / Fernet); `/setup` command group (faction, torn-key with live validation, ffscouter-key, role, show, disable); `MultiGuildWarRunner` (per-server `WarManager` + own Torn rate budget, shared FFScouter budget, cache rebuilt on config change); global + per-server-on-join command sync; `wars.guild_id`; guild-scoped `/war` etc. | ✅ bot boots multi-tenant, connects, global-syncs; 109 tests | ✅ (live `/setup` pending an invited server) |
| **5 — Live updates** | `EnemyRosterService.refresh_with_diff` (join/leave detection, `fetch_estimates` toggle), `WarLogEvent` generation (state changes, war finished, roster join/leave), `WarLogBoard` (append-only `#war-log` posts), two new scheduler cadences (`status`/`stats` refresh), `/refresh` (rate-limited, on demand) | Discord is a live war dashboard | ✅ (live run pending an invited server) |
| **6 — Targeting** | `app.war.targeting.rank` (pure, weighted, availability-gated), `TargetingService` (roster -> persisted `target_rankings` snapshot), `target_board_embed`, `/targets` (with an `available_only` filter), wired into every roster build/refresh | Priority target list in `#target-board` | ✅ (live run pending an invited server) |
| **7 — War history** | `WarHistoryService.recent` (guild-scoped read of already-finished wars), `history_embed`, `/history` — finished-war detection, result capture and roster retention were already side effects of Phases 2-3 | `/history` shows past wars | ✅ |
| **8 — Advanced intelligence** | `/spy add/show/remove` (paste a report; per-server `confirmed_stats`), a spy total under 14 days old outranks the estimate on the board / `/targets` / `/matchups` (🔍), `/matchups` (our player vs every enemy: good / even / avoid), `/config` dropped (`/setup` is the one config place). In-house estimator: still design only | Spies + matchups in Discord | ✅ (estimator ⬜) |

## Non-goals for the MVP
- Machine-learning estimator (collect the data first — spec §23).
- Multi-guild / multi-faction support (`/setup` in DB) — `.env` config only for now.
- Redis / horizontal scaling — single instance, in-process cache (spec §35).
