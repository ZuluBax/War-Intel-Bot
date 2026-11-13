# PROJECT_STATE

Shared source of truth. Read before starting work; update on any milestone or
architectural change.

## Status: LIVE on Raspberry Pi 5 (since 2026-11-10 08:47 UTC, commit at `master` 10 Nov) — Railway retired

- **Where:** `<user>@<pi-host>` (Pi 5, Debian 13 trixie, aarch64; IP changes,
  use the `.local` name), `~/torn-war-intelligence`, `docker-compose.pi.yml` (bot only),
  database still **Supabase**. Docker 29.9 + Compose v5.7, enabled on boot; container
  `restart: unless-stopped`. See DEPLOYMENT.md "Option C" for update/rollback commands.
- **Why moved:** Railway's free plan hit its usage hard limit and **removed every
  deployment** — the bot was offline from some time after 15 Oct until 10 Nov (exact
  start unknown; Railway kept no logs). Also: a 15 Oct Railway deploy (89a60330, made
  from a Claude Code session) was never recorded here; it probably shipped Phase 8, which
  is why `confirmed_stats.guild_id` was already present (no `column_added` this time).
- **Secrets:** `.env` on the Pi (mode 600) built from Railway's variables without printing
  them; local copy `.env.pi` (git-ignored). `CONFIG_ENCRYPTION_KEY` must also be kept in
  the owner's password manager — Railway was the only other copy.
- **Startup check (08:47 UTC):** clean — `database.schema_ready`, `discord.ready`
  guilds=2, global + both servers' commands synced, no `crypto.key_mismatch`, no
  `war.stale_war_closed` (no stuck wars). Guild <NV_GUILD_ID> registered war 52019
  vs Dark Angels Elite as already WAR_FINISHED (fought while the bot was offline:
  history only, no hits/summary).
- **Action needed:** guild **<TEST_GUILD_ID>**'s saved Torn key is rejected by Torn
  (`TornAPIAuthError: Incorrect key`, every minute) — its admins must run
  `/setup torn-key` with a current key. Not a bot fault.
- **Rollback:** previous code is commit `7ae67fa`; on the Pi: re-copy that commit with
  `git archive 7ae67fa | ssh ...` and rebuild.

## Public on GitHub (13 Nov 2026) — https://github.com/ZuluBax/War-Intel-Bot (branch `main`)

- **Public repo.** Owner's choice: keep it public, with Discord IDs and Pi details masked.
  The docs use placeholders (`<NV_GUILD_ID>`, `<TEST_GUILD_ID>`, `<BOT_CLIENT_ID>`,
  `<user>@<pi-host>`, …); the real values are in the git-ignored `PRIVATE_NOTES.md`
  (local only). **Never commit real server/channel/role IDs, hostnames or keys.**
- **History:** GitHub holds one snapshot commit on top of its MIT `LICENSE` commit, not
  the 89 development commits, so the old IDs and personal email aren't published. The
  full private history is kept locally on branch `archive/private-history`. Commits use
  the GitHub no-reply email (set in this repo's git config).
- **Licence:** MIT (chosen when the GitHub repo was created; replaces the old "TBD" file).
- Deploy from `main` now (`git archive main | ssh ...`, DEPLOYMENT.md Option C). The
  Pi runs the same code as `main` (deployed 13 Nov 09:47 UTC; only docs differ).

## Target board split over two messages — LIVE on the Pi since 13 Nov 2026 09:47 UTC

- **Deploy check:** clean start (`discord.ready` guilds=2, commands synced in both
  servers); `target_board.trimmed` stopped; Crossfire's war 8 board now has a second
  message (`extra_message_ids` set). Rollback: previous code = `archive/private-history`
  commit `4a3517b`.

- **Why:** Discord caps a message at 6000 characters. The test server (guild
  <TEST_GUILD_ID>) now tracks **Crossfire (37530)**; its war vs faction 9047 has 87
  enemies and the board was shortening every line and still dropping 17
  (`target_board.trimmed total=87 shown=70`).
- **Now (owner's choice, keep full detail):** full lines in one message if they fit,
  else TWO messages titled `(1/2)` / `(2/2)` with as near equal enemy counts as fits
  (cut tried at the middle, then up to n/10 rows either side). A section cut in two
  repeats its header in part 2 with "— continued". Overdoses end the last part. Only if
  two messages can't hold it: shortened lines, then rows trimmed (largest count found
  by halving — 250 enemies renders in 0.15 s, was 12 s with one-at-a-time). The country
  picker sits under the last part (part 1 gets an empty view so an old picker goes).
- **Posting:** `WarBoard.publish_messages` (`publish` now wraps it). Part 2's id lives in
  the existing `discord_channels.extra_message_ids` column (already in the live DB — no
  schema change). Shrinking back to one message deletes part 2 (`GuildGateway.remove_message`,
  new). If part 1 has to be re-posted (deleted by hand), part 2 is re-posted after it so
  the order stays 1 then 2. Ids are saved even if posting fails part-way. Moving a board
  to another channel clears both ids. `country_embed` stays one message.
- **Tests:** new cases in `test_target_board.py`, `test_war_channels.py`,
  `test_overdose.py`; one existing test changed on purpose (200 enemies now = 2 messages,
  still trimmed with a note). Every test file passes (run one file at a time), ruff +
  mypy clean.
- **Known:** in single-channel mode, a newly posted part 2 lands at the channel bottom,
  so another board could sit between the two parts until part 1 is re-posted.
- **Live check after deploy:** `target_board.trimmed` should stop for the 87-enemy war;
  #target-board shows `(1/2)` and `(2/2)` with all 87 names and the picker under part 2.
- **Local dev gotcha (13 Nov):** on the owner's PC, Python crashes silently (exit
  45/207) whenever the current directory is on `sys.path` and package metadata is
  scanned, so `pytest` / `python -m pytest` die with no output. `.venv` was rebuilt with
  `uv sync --frozen --all-extras`; it didn't help. Workaround:
  `.venv/Scripts/python.exe -P -c "import pytest; raise SystemExit(pytest.main(['-q','-p','no:cacheprovider','tests/test_x.py']))"`
  (one file per run; the whole suite in one process still crashes). Root cause not found.

## Review fixes on branch `fix/review-medium-issues` — deployed to the Pi 10 Nov 2026

From a code review; ship together with Phase 8. 538 tests pass (the suite was 523
before, not the 524 noted below), ruff + mypy clean. No schema change.
- **One job at a time per server** (`MultiGuildWarRunner._lock_for`): war-detect, stats
  refresh and `/refresh` wait for each other; the frequent status refresh *skips* a busy
  server instead of queueing. Overlap used to double-post roster join/leave events and
  duplicate `target_rankings` rows. Lock lives in the runner because a config change
  rebuilds the `WarManager`.
- **#war-log can't jam:** a message Discord rejects (400, e.g. too large) is logged as
  `war.log_event_rejected` / `war.hits_message_rejected`, marked
  `posted_message_id = 0` (`REJECTED_MESSAGE_ID`) and skipped. 403/404/5xx stop the pass
  and retry later with nothing dropped (`war.log_send_blocked`, logged once).
  A war summary rejected part-way leaves the parts already posted.
- **FFScouter key never logged:** httpx/httpcore pinned to WARNING even at DEBUG, and the
  redactor masks `key=`/`api_key=`/`apikey=`/`token=` in any logged URL.
- **Missed war ends are closed** (`WarManager._close_stale_wars`, branch
  `fix/auto-close-stale-wars`): the detector only follows Torn's *newest* war, so a war
  whose end the bot missed (offline when it ended, next war already announced) used to
  stay open forever. Each war check now looks any other open war up in Torn's
  ranked-war list: finished there -> closed with the real result + war-finished event +
  end-of-war summary/CSVs (owner chose: post it, even if hits are incomplete); missing
  from a non-empty list -> closed, result unknown ("The war has ended."); still running,
  empty list, or Torn error -> left open. `get_active` now returns the newest open war.
  546 tests pass.
- **First deploy:** any war already stuck open in the live DB gets closed on the first
  war check and posts a late summary to #war-log (look for `war.stale_war_closed`).
- **Wrong encryption key stops the bot loudly** (`GuildConfigService.startup_key_check`,
  branch `fix/encryption-key-guard`): at startup, if API keys are saved and the current
  key can read **none** of them (key changed, or unset so a new `config/secret.key` was
  generated), log `crypto.key_mismatch` and exit 1 — nothing is modified. If only some
  are unreadable, log `crypto.some_keys_unreadable` and start. Emergency way out when the
  original key is lost: `RESET_UNREADABLE_KEYS=true` for one start clears only the
  unreadable keys (faction/role/channels kept); those servers re-run `/setup torn-key`.
  Remove it afterwards. 552 tests pass.
- **Still open from the review:** the low-priority list.
- **Live check after deploy:** no `war.log_send_blocked` / `*_rejected` lines expected
  normally; `/refresh` still answers during a stats pass (it now waits its turn).

## Phase 8 built (10 Oct 2026) — live (probably Railway 15 Oct; certainly the Pi 10 Nov)

Built while the 9 Oct war was running; held back so a restart doesn't blank the boards
mid-war. Deploy after the war (or when the owner says). 524 tests pass, ruff + mypy clean.
- **`/spy add player:`** opens a paste box (a modal keeps line breaks). `app.war.spy.parse_spy_report`
  reads Strength/Defense/Speed/Dexterity/Total (commas, `4.5b`/`300m`, `N/A`, one-line pastes).
  Rejects text with no stats, and a report whose `[ID]` is a different player. A bare unknown
  Torn ID is added as a Player. `/spy show`, `/spy remove` (deletes the newest; older returns).
- **Per server:** `confirmed_stats.guild_id` (new nullable column, auto-ALTERed on startup).
  Every read filters on it — one faction's spies never show in another server. No index on it
  (create_all won't add one to an existing table; the table is tiny).
- **Spy vs estimate:** `best_strength` — a spy total <= 14 days old (`SPY_TRUSTED_FOR`) is
  used instead of the FFScouter estimate for ranking, the target board, `/targets` and
  `/matchups`, marked 🔍. Older: estimate used, card says so. Never written into `stat_estimates`.
- **`/enemy` / `/stats`** show the server's newest report in its own "🔍 Spy report" field.
- **`/matchups [player]`:** you = `[ID]` in your server nickname, or the option. Your strength =
  trusted spy, else one FFScouter call (`MultiGuildWarRunner.strength_estimate`, cached 30 min),
  else an old spy. Enemies from the saved ranking (no Torn calls). Good < 75% of you, even
  75-125%, avoid > 125% (`app.war.matchups`). Totals only — a rough guide.
- **`/config` removed** (and `not_implemented_embed`); Discord drops it on the next global sync.
- **Live checks after deploy:** column added (`database.column_added confirmed_stats.guild_id`),
  `/spy add` with a real Torn spy paste (the parser has only seen made-up text), `/matchups`
  from a member whose nickname has `[ID]`.
- **Not done:** in-house estimator design note (deferred by the owner's scope pick).

## Status: LIVE on Railway (deployed 2026-10-09 08:21 UTC, deployment 72778fa4, commits 3542e38 + b7dddaa) — 403 back-off + enemy overdose tracking

- **Deploy check (08:21-08:22 UTC):** clean start, `overdoses` table created, commands synced
  on both servers, boards publishing, 0 errors / 0 x 403 / 0 x 429 in the first minute (only
  the usual "davey not installed" voice warning). Rollback target: deployment 1c99e9dd.
- **First live overdoses (proves the `details` match):** DoctorB (1796785) and Tomspy77
  (4408626), Xanax, wars 5 + 6 (same enemy, both servers). Checked against status history:
  both stays began after the 6 Oct 12:04 announcement, so `before_watch=False` is right.
  Their `first_seen_at` was the deploy time (feature not live yet); backdated by hand on
  9 Oct to the first hospital snapshot of the stay (DoctorB 8 Oct 19:43:54, Tomspy77 9 Oct
  01:24:24 UTC; +4s on war 6). OD stays seen here were 74h59m, not 80-83h — don't key on length.
- **N&V 429s — resolved by their own switch, not code (9 Oct):** N&V (guild <NV_GUILD_ID>,
  war 6) has run `/setup board` in 5 admin-made channels (`guild_board_channels`), so boards
  no longer share one channel's edit bucket. 0 rate-limit / skipped-job lines in the first
  ~23 min after deploy (was ~5/min). **Confirmed in war (20:00-20:28 UTC, 9 Oct):** war went
  WAR_ACTIVE 20:00:41; 0 Discord 429s, 0 skipped/late jobs; target board published 117x on
  war 6 vs 113x on war 5. Only errors: 9 *Torn API* "error 5: Too many requests" lines
  (war.refresh_failed/hit_sync_failed), war 5 (test server) only, 20:00-20:08, then cleared
  on their own — the war-start burst on that server's key; watch if it recurs. Slower single-channel refresh
  NOT built — no server uses single-channel mode; build it only if one goes back to it.
- **403 back-off (3542e38):** per-war mode checks Manage Channels (from cache) before
  creating anything. Without it: no create is tried, boards keep posting in whatever
  channel they already had (N&V: back to #war-utilities), one `war.manage_channels_missing`
  warning. A create Discord refuses anyway backs off 10 min (`_CREATE_RETRY_SECONDS`,
  in memory — a restart retries once). Still no in-Discord notice to the server's admins.
- **Overdoses (b7dddaa):** owner wanted enemies who overdosed since the war was announced.
  Torn's public status `details` reads "Overdosed on Xanax" for the whole ~80-83h stay
  (owner-confirmed wording), and the roster is polled every minute from announcement, so
  no extra API calls. New table `overdoses` (one row per stay; `hospital_until` kept
  current so a meds-shortened stay ends the episode). Shown: #war-log line (🤢), a
  section at the bottom of #target-board kept all war, and a box after the end-of-war
  scoreboard. Already under way at the first look -> `before_watch`, kept off all three.
  **Not caught:** overdoses that don't hospitalise (Ecstasy). Optional later: Torn
  personal-stats `overdosed` counter (unverified key; ~1 call per enemy per check).

## Status: LIVE on Railway (deployed 2026-10-01 19:54 UTC, deployment 1c99e9dd, commit df91314) — `/setup board`: bring-your-own channels, no Manage Channels

- **Deploy check:** clean start, schema ready (new table created), `/setup board` synced to
  both servers (10 commands each), test server publishing normally.
- **N&V at deploy time (not caused by it):** at 19:49:54 UTC someone in N&V cleared
  `/setup channel` with Manage Channels already removed, so per-war mode tries to create
  channels every 15s -> `403 Missing Permissions (50013)`; information + stats boards stop
  updating (targets/activity still edit the old #war-utilities messages). Resolves once they
  run `/setup board` in their own channels. Possible follow-up: back off / tell the admin
  instead of retrying a forbidden create every tick.

- **Why:** N&V's owner won't give the bot Manage Channels (trust). Wants the 5-channel
  layout anyway, with channels they made themselves.
- **How:** admin makes `war-information`, `enemy-stats`, `target-board`, `war-log`,
  `enemy-activity`, runs `/setup board` in each. Board is guessed from the channel name
  (`board_for_channel_name`, also accepts the bot's emoji-prefixed names) or picked via
  the `board` option; `remove:True` unassigns. Same permission check as `/setup channel`.
- **Storage:** new table `guild_board_channels` (guild_id, purpose, channel_id), created by
  `create_all` on startup. Carried as `ResolvedGuildConfig.board_channels`, part of the
  runner's cache signature so changes apply on the next tick.
- **Mode rules (`WarChannelManager._ensure_own_channels_workspace`):** any board row set
  -> never create anything. Unassigned boards fall back to the `/setup channel` room if
  set, else aren't posted. A deleted/hidden assigned channel is skipped with
  `war.board_channel_missing` (other boards carry on).
- **Invite without Manage Channels:** permissions `117760` (link in `docs/INSTALL_GUIDE.md`).
  Existing servers can just switch the permission off on the bot's role.
- **N&V switch-over:** they can do it mid-war; boards not moved yet keep posting in
  #war-utilities. Old posts there stop updating — delete by hand.

## Status: LIVE on Railway (deployed 2026-10-01 18:24 UTC, deployment c3de62eb, commit e94a475) — duplicate #war-information channels fixed

- **Incident (test server, guild <TEST_GUILD_ID>, war row 3, 1 Oct):** a new
  #war-information channel every few minutes (18:10, 18:16, 18:20, 18:21 UTC).
- **Cause:** e79d12e's "channel held by 2+ purposes = leftover /setup room" check counted
  the `score` row, which shares #war-information by design. So each refresh made a new
  information channel, the score box followed it, and it repeated.
- **Fix:** the owner count ignores `SCORE_PURPOSE`. Regression test in `test_war_score.py`.
- **Also:** `WarBoard` skips an edit whose embeds+view are unchanged (re-edits every
  5 min anyway so a hand-deleted board is reposted); `edit_embeds` uses a partial message
  (1 call, not fetch+edit).
- **Deploy check:** 0 `war.channel_created` in the first 4 min. Owner to delete the
  leftover #war-information channels by hand (keep the newest).
- **Still open:** N&V single-channel mode still gets Discord 429s (~5/min) on the
  targets + stats boards — their content really changes every 15s and all boards share
  one channel's edit bucket, so the 15s job sometimes overruns and skips. Option: slower
  refresh for those boards in single-channel mode. Not decided. **-> Resolved 9 Oct by N&V
  moving to `/setup board` (see top entry).**

## Status: LIVE on Railway (deployed 2026-09-29 19:28 UTC, deployment fad60977, commit 8ad69e8) — flights out listed under their destination country

- **Why:** owner wants each ABROAD country to also show who is on the way there.
- **How:** `_section()` in `app/discord/target_board.py` puts OUTGOING rows (Torn ->
  country) under that country in ABROAD, below those already there, soonest landing
  first. Header `🇯🇵 __**Japan**__ (2 + 1 flying in)`; row detail `flying in · lands …`
  (not clickable). FLYING OUT now only catches a flight with no destination (can't
  happen with Torn's text today). Picker counts + private country view include flyers.
  INCOMING (-> Torn) unchanged.
- **Deploy check (19:28 UTC):** clean start, boards published in both servers, no errors.

## Status: LIVE on Railway (deployed 2026-09-29 12:45 UTC, deployment d5338193) — `/setup channel` refuses channels the bot can't post in

- **Incident (Null & Void, guild <NV_GUILD_ID>, war row 4, 29 Sep):** setup at
  12:12 worked; its own category + 5 channels filled at 12:13. At 12:15 an admin ran
  `/setup channel #war-utilities`. That channel denies @everyone View Channel and has no
  overwrite for the bot role (`War Intel Bot` <BOT_ROLE_ID>), so from 12:16 every
  publish failed `403 Missing Access (50001)`. The bot's own category was later deleted.
- **Fix:** `/setup channel` checks View / Send / Embed Links / Read History for the bot
  and refuses (names what's missing) instead of saving. `upsert_channel` clears
  `live_message_id` when a purpose moves to a different channel.
- **N&V then let the bot into #war-utilities** (posting worked, but all 5 boards in one
  channel). **Second fix (deployed 18:52 UTC, deployment ad765ad4, commit e79d12e):**
  clearing `/setup channel` mid-war kept reusing the shared channel for all purposes
  (the rows still pointed at it and it exists). Per-war mode now skips a channel held
  by 2+ purposes, so the 5 channels are created.
- **N&V decided (29 Sep) to stay in single-channel mode (#war-utilities)** — not
  running `/setup channel` to clear it. Their choice; nothing pending.
- **If they ever switch:** `/setup channel` with the box empty. Verify in logs:
  `war.category_created` + 5x `war.channel_created` for war_row_id=4. Old posts in
  #war-utilities stop updating and can be deleted by hand.
- N&V's bot role has perms `85008` (old invite, no Attach Files) — re-invite with the
  `117776` link in `docs/INSTALL_GUIDE.md` to get the end-of-war spreadsheet.
- Admin-facing guide: `docs/INSTALL_GUIDE.md` / `.txt`.
- Open: guild <TEST_GUILD_ID> (war row 3) hits Torn `error 5: Too many requests`
  every 2-3 min since ~12:11; not investigated yet.

## Status: LIVE on Railway (deployed 2026-09-24 18:32 UTC, commit cad5885) — `#target-board` ABROAD grouped by country + private country picker

- **Why:** owner wanted each country as a header with its people under it, foldable, so
  they can look at one country only (e.g. Japan) without the rest.
- **Discord cannot fold sections** (no collapsible component; checked Discord's
  component reference 24 Sep 2026), and the board is one message everyone shares.
  Owner chose: headers on the board + a private picker.
- **Board:** ABROAD = one sub-list per country A-Z, header `🇯🇵 __**Japan**__ (2)` (bold +
  underlined: kept on purpose so it doesn't blend with bold online names). Hawaii = 🌺.
  Rows no longer repeat "in Japan". Country names are Torn's 11, checked against
  `player_status_history` (unknown future ones get 🌍).
- **Picker:** drop-down under the board (`app/discord/abroad_picker.py`, custom_id
  `twi:abroad-country`), options = countries with enemies now. Pick -> ephemeral
  one-country list (snapshot; pick again to refresh). No intel-role check: it only
  repeats what the board shows. Handled in `TornWarIntelBot.on_interaction` by
  custom_id, no registered view, so it works after restarts. Empty view removes it
  (nobody abroad / war over). `GuildGateway.send_embeds/edit_embeds` and
  `WarBoard.publish` gained `view=` (None = leave components alone).
- **Deploy check (18:32 UTC):** clean start — discord.ready, emoji reused, 10 commands synced in both servers, refresh jobs OK, no errors. No war on, so the board itself is untouched until the next one.
- **Deploy check (18:32 UTC):** clean start — discord.ready, emoji reused, 10 commands synced in both servers, refresh jobs OK, no errors. No war on, so the board itself is untouched until the next one.
- **Not yet seen live:** whether the 15 s board edit snaps an open drop-down shut. Check
  in the Tue 29 Sep war; if it's annoying, only re-send the view when choices change.

## Status: LIVE on Railway (deployed 2026-09-24 11:26 UTC, commit dbffe9a) — `#target-board` warns when enemies heal out of hospital early

- **Why:** user wants to *see*, as it happens, enemies using meds to cut a 3-4 h hospital
  timer (or getting revived) so they can come back and attack. A ping was judged too slow.
- **How:** `app/war/hospital_watch.py` compares each enemy's hospital release time with
  the previous refresh (in memory, no extra Torn calls). Release time moving 3+ min
  earlier = a medical cut. Leaving hospital with 2+ min left = out early; 4h+ left =
  `REVIVED`, less = `OUT EARLY` (one big item could explain it). Flag shows 10 min.
- **Board:** 1 cut -> pulsing amber warning + `💊 -1h 10m`; 2+ cuts in one stay or out
  early -> spinning red siren. Alerted rows go to the top of their section. Minutes cut
  only, never an item name (drops were 120/132/140/144... not clean item sizes).
- **Emoji:** own artwork (`scripts/make_alert_emoji.py` -> `app/discord/assets/*.gif`),
  uploaded as **application emoji** by `app/discord/alert_emoji.py` on first `on_ready`
  (no Nitro / external-emoji permission needed). Already uploaded 24 Sep:
  `twi_warning_pulse` <EMOJI_ID_WARNING>, `twi_siren` <EMOJI_ID_SIREN>. Unicode
  ⚠️/🚨 fallback if the upload ever fails (log `alert_emoji.unavailable`).
- **Checked on the 15-22 Sep war (171 h):** would have shown 78 warnings, 8 sirens
  (max 3 cuts in one stay), 404 early exits into Torn.
- **Limits:** a bot restart forgets cuts for stays in progress; two items within one
  refresh look like one bigger cut; no verdict across a 3+ min gap in observations.
- **Faster refresh (same change set):** `status_refresh_interval_seconds` default 60 -> 15
  (the validator's floor; not set on Railway, so the default applies). Torn caches
  answers up to 30 s — confirmed live 24 Sep (same answer 12 s apart); its API page
  allows bypassing with a `timestamp` param, now sent on `/faction/{id}/members`
  (confirmed live: fresh answer, no error). ~4 member calls/min per server.
- **Status history only on change:** `player_status_history` grew 79 MB in one war at
  60 s; 15 s would fill Supabase free (500 MB, read-only past that) in ~2 wars. Rows are
  now written only when state / release time / plane / online label (or non-countdown
  text) changes — 2.8% of last war's rows. Every reader wants only the latest row.
- **Deploy check (11:26 UTC):** startup log shows `war-status-refresh` at 15 s and
  `alert_emoji.ready` reusing both emoji; no errors in the first 200 log lines.
- **Next:** watch the Tue 29 Sep war: logs `hospital_watch.med_drop` /
  `hospital_watch.out_early`, that the emoji animate on the board, and for APScheduler
  "maximum number of running instances reached" (a refresh taking over 15 s — then
  the board just updates every other tick; raise the interval if it is constant).

## Status: LIVE on Railway (deployed 2026-09-22 22:00 UTC, commit c45bd4a) — `#enemy-activity`: when the enemy is busy / quiet

- **Why:** replaces FFScouter premium's Activity Tracker (user letting premium lapse 22 Oct).
- **How:** every roster build (1/min from war *announcement* — Tuesdays ~12:00 TCT, war
  starts 2-3 days later) stores each enemy's `last_action` 5-min slot (`player_activity`,
  kept across wars) and a watch slot per war (`activity_watch`) so downtime is "not
  watched", never "quiet". No extra Torn calls. `app/war/activity.py` (pure maths),
  `activity_service.py` (DB), `app/discord/activity_board.py` (the post).
- **Post (every 15 min, new 5th channel `🕒-enemy-activity`):** active now, 3 busiest /
  3 quietest TCT hours (only once 6+ hours have 30+ min of data), 24-hour bar chart,
  usual online hours of the 10 strongest enemies. "Still learning" under 12 h watched.
- **Verified vs FFScouter (22 Sep 21:45-23:30 UTC, polled every 60 s):** per-15-min
  active counts identical in 12 of 14 buckets (Mayans MC 50228, Big Wheelers 1251), off by
  1 in the other 2; per player per 5-min slot (Mayans MC, 48 x 21 slots) 99.4% agreement —
  6 slots we saw and FFScouter didn't, 0 the other way. First real use: Tue 29 Sep.
- Tests with hard-coded "4 channels" now use `len(CHANNEL_LAYOUT)`.
- **Lock (deployed 22:11 UTC, commit 5e1dff1):** new channels copy the hand-locked
  category's permissions at creation, trimmed to bits the bot holds (the live lock also
  denies TTS/voice messages, which the bot lacks — a full copy would be refused).
  **Check on Tue 29 Sep** once `🕒-enemy-activity` appears: @everyone must have Send ✗.
  If it ever comes out unlocked (log `gateway.channel_lock_not_copied`): channel settings
  -> Permissions -> "Sync now" with the category.

## Status: LIVE on Railway (deployed 2026-09-22, commit 0fd5b67) — the bot's own landing windows (FFScouter premium no longer needed)

- **Why:** user asked to replicate FFScouter's paid Flight Tracker so they need not keep
  paying for premium. With a 30-day premium key we pulled 470 real flights and found
  FFScouter's method is just *take-off time + route time x a per-plane range*.
- **What:** `estimate_windows()` in `app/war/live_status.py` gives a landing window
  (fastest-slowest) per plane, fitted to those 470 flights: PI 99% inside a median
  11-minute window (FFScouter: 99%, ~10-12 min). Board: `~ lands in 12 min (12:10-12:21) PI`.
- **Airliner ambiguity:** Torn shows normal ticket and BCT (~3x faster) both as
  "airliner". New `flight_records` table (auto-created by `create_all`, no migration)
  stores every flight the bot watched from take-off to landing; the enemy's last
  airliner ticket picks one window. No history -> both windows shown until BCT passes.
- **FFScouter premium** is now only a gap-filler for flights whose take-off the bot
  missed (🎯). FFScouter's own "BCT" label was wrong for 24 of 34 flights in the sample.
- **Honest limits:** ranges were fitted on the same 470 flights (in-sample); Airline
  (n=44) and BCT (n=10) are thin; WLT unmeasured (Torn's rule). A live out-of-sample
  landing check on 22 Sep (7 flights, new to the fit, landings watched every 30 s):
  bot's windows 7/7, FFScouter 7/7, near-identical windows. Small sample; it used
  FFScouter's take-off times (the bot's own are within ~60 s, allowed for in the window).
- **Re-fit + bigger tests (same day):** re-fitted on 3,672 flights (53228 + 7 past
  opponents). The earlier fit scored 98.3% vs FFScouter 97.7% on 3,202 unseen flights.
  Final fit on a hold-out of 1,252 flights (Mayans MC 50228, Big Wheelers 1251 — the
  user gave "3951071" for Big Wheelers; that's co-leader Reyz's player ID): 99.8% vs
  FFScouter 99.0%. WLT 10/10, BCT 10/10 (FFScouter 8/10).
  Airliner times changed in Torn ~20-25 Jun 2026 (1.000x -> 0.947x): pre-June flights
  look like misses; exclude them from any re-fit.
- **End-to-end (real bot code, live Torn, 90 min, 22 Sep):** 32 take-offs spotted by the
  bot itself; 5 of 5 recorded landings inside the window predicted at take-off.
- **Next:** after deploy, compare `flight_records` against the table monthly (the log
  line `roster.flight_recorded` has minutes + method); add real WLT figures.

## Status: LIVE on Railway (deployed 2026-09-22, commit 0fd5b67) — FFScouter premium landing windows on `#target-board` (now gap-filler only)

- **What:** enemies in the air show FFScouter Flight Tracker's landing window
  (`🎯 lands in 10 min (12:10-12:14) · WLT`) instead of our own `~` estimate, when the
  server's FFScouter key has **premium** (1 Xanax / 15 days, paid in Torn). Covers
  flights the bot never saw take off — our biggest gap ("landing time unknown").
- **How:** `app/war/flights.py` `FlightTracker.enrich()` runs after targeting, before
  the board publishes. Asks `/player-flights/batch` only about players flying (not
  sitting abroad); caches per player until the flight changes or 5 min pass; budget
  limiter is per FFScouter *key* (shared across servers), non-blocking.
- **No premium:** tracker turns itself off for 1 h at a time; board unchanged.
- **Bug found + fixed on the way:** FFScouter's live error body is
  `{"code": N, "error": "..."}` (code at top level), not the documented nested shape —
  so real bad-key / rate-limit errors on `/get-stats` were never classified before.
  Now both shapes are handled. Verified with one live call (non-premium key -> code 19).
- **Not yet verified live:** the premium happy path (no premium key available). The
  parser is tested against FFScouter's documented example reply.
- **Not done (next):** `/targets` (the slash command) reads from the DB so it does not
  show 🎯 windows — only the `#target-board` channel does. Activity Tracker
  (`/activity/*`, "when is this enemy usually online") is a possible v2.

## Status: LIVE on Railway (deployed 2026-09-21, commit b5b3414) — live score box in `#war-information`

**Deploy verified 2026-09-21:** deployment `2d65d8d7` SUCCESS (Railway made two builds 4 s apart, `a5544db6` and
`2d65d8d7`; the older one was replaced — same code). Clean startup. On the first tick Discord was not connected yet, so
the log showed `war.publish_deferred purpose=score` (expected); the next tick (21:40:03) posted the box and
`discord_channels` got the `score` row with a `live_message_id`. Not yet seen by eye in Discord: how the box looks.

Owner asked for a small box in the war information showing both factions' current score, updating live.
- **What it is:** its own small message under the big info box, edited in place (never re-posted). Title `📊 LIVE SCORE`,
  one field per faction plus `Target`, and a line "Leading / Trailing by N" / "Tied". Green ahead, red behind, grey level.
  When the war ends it becomes `🏁 FINAL SCORE` with the result.
- **Source, cost:** the `score` on each faction in Torn's `/rankedwars` response, which `WarManager.tick()` already
  fetches every `war_check_interval_seconds` (60 s, 15 s in the fast window). **No extra Torn calls.** So "live" means
  "as fresh as the last war check", not second-by-second. Scores are not stored in the DB.
- **Where it lives:** `app/war/score.py` (`WarScore`), `war_score_embed` in `app/discord/embeds.py`,
  `DiscordWarPublisher.publish_score`, called from `WarManager._publish_score` at the end of every `tick()`.
  It has its **own `discord_channels` row, purpose `score`, pointing at the information channel** (`SCORE_PURPOSE` in
  `war_channels.py`) — deliberately not in `CHANNEL_LAYOUT`, which would create a fifth channel. No schema change
  (a row, not a column), so rollback is safe: old code just ignores the row.
- **`WarBoard.publish` now returns `bool`** (True once on Discord, False if skipped/deferred). Existing callers ignore it.
- **Failure handling:** missing score shows `—` (never a fake 0); a deleted box is re-posted next tick; the final score
  is kept pending (`_final_score_pending`, in memory) until Discord accepts it. A war first seen already finished gets no
  box (it never had a workspace). Not shown before the war starts (0 - 0).
- 371 tests pass (20 new); ruff + mypy clean.
- **Known gap:** if the bot restarts *after* a war ended but before its final score posted, the box keeps the last live
  score (the pending set is lost). Rare; fix would be storing the final score on the `wars` row.

## Status: LIVE on Railway (deployed 2026-09-20, commit a1d6a13) — chain number + gold milestone boxes in `#war-log`

**Deploy verified 2026-09-20:** deployment `c66dc90e` SUCCESS; startup logs clean; right after start
`war.hits_synced new_hits=2` and `war.hits_published count=1` (a hit went out with the new code). Not yet seen by
eye in Discord: the `Chain N` text and the first gold milestone box (needs a milestone hit to land).

Owner asked where hits are logged, and for the chain to be counted with milestone hits highlighted.
- **Where hits are logged:** stored in the `war_hits` table (Supabase), posted to `#war-log` by
  `WarLogBoard.publish_pending_hits`; one line per hit, built in `app/discord/hit_embeds.py`.
- **Chain:** every hit line now ends `… · Chain 380 · RW: no`. The number is Torn's own `war_hits.chain`
  (already stored; nothing new is fetched). Not shown when `chain` is 0 (loss / assist) or NULL (legacy row).
- **Milestones:** `CHAIN_MILESTONES` = 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000
  (25 added at the owner's request after it was first left out; Torn pays a bonus there too).
  A milestone hit gets its **own gold box** titled `🏆 CHAIN 100 · war hit` (or `outside hit`) and a bold
  `🏆 **CHAIN 100**` on its line. It splits the surrounding green/orange run in two, so time order is kept.
- **Checked on the live DB (read-only):** `chain` is the chain's number *after* the hit — the 10/25/50/100/250/500th
  hits carry exactly Torn's bonus respect (10/20/40/80/160/320). Rendered 5 real hits round the 500th: correct.
- **Quirk seen in real data:** Torn numbers the chain when a fight *ends*, the log orders by when it *started*,
  so a long fight can show one line out of chain order (500 shown just before 499). Cosmetic; left as is.
- Only hits posted **after** the deploy get this; old lines are not edited. 351 tests pass (7 new); ruff + mypy
  clean; switching the milestone list off fails 4 tests.

## Status: LIVE on Railway (deployed 2026-09-20, commit 4bd03bf) — online players highlighted on `#target-board`

**Deploy verified 2026-09-20:** deployment `bae2b827` SUCCESS; clean startup; `war.targets_published` for all 49
enemies. A read-only query then showed `player_status_history.online_status` filling in (last 5 min:
Online 18 / Idle 10 / Offline 70 rows; older rows are NULL). Not yet seen by eye in Discord: the bold + ⚡ look.

**Landing-estimate check #1 (result):** the Torn -> Canada `light_aircraft` flight first seen taking off at
11:17:19 UTC was predicted to land at 11:44:22 (41 min x 0.66). It was still in the air at 11:44:30 and the
next look (11:45:30) showed the same player already flying back — so it landed 8-68 s AFTER the estimate,
on a ~27 min flight (observed 27.2-28.2 min vs estimated 27.06). One flight only: keep collecting before
trusting it broadly, and check the `airliner` / `private_jet` factors separately (none seen yet).
The return flight (Canada -> Torn, first seen 11:45:30) is a second data point to read once it has landed.

Owner asked to highlight players who are online. Online players get a **bold name + ⚡** on every
kind of line (available, hospital, abroad, flying); the footer explains `⚡ = online now`.
- **Source:** Torn's own `last_action.status` ("Online" / "Idle" / "Offline"), stored as
  `FactionMember.online_status` → `RosterEntry` → `TargetView`, and in new nullable column
  `player_status_history.online_status` (added automatically at startup; old code ignores it, so
  rollback is safe) so `/targets` shows the same. Only "Online" is highlighted.
- **Checked on real data (one live call):** of 49 enemies 11 Online (all active <= 9 min ago), 5 Idle,
  33 Offline. **Torn's label is not just a time cut-off** — some Idle / Offline players have a
  last action only 2-3 minutes ago — so the label is used as sent, never recomputed from the timestamp.
  Highlighting Idle as well would be a one-line change if wanted.
- Size: with ~12 of 49 online it is still one box, 5150 of 6000 characters, full lines.
- 344 tests pass; ruff + mypy clean; four deliberate breaks (highlight offline / also Idle / stop
  reading the label / `/targets` forgets it) each fail a test.
- Not seen by eye in Discord: how the bold + ⚡ reads.

## Status: LIVE on Railway (deployed 2026-09-20, commit 87d8ec4) — `#target-board` is ONE box (not two)

**Deploy verified 2026-09-20:** deployment `19a5b13a` SUCCESS; clean startup; `war.targets_published` for all 49
enemies, no `publish_targets_failed`. The existing message was edited in place from 2 embeds to 1.

Owner: the board showed as 2 lists. Cause: it was sent as 2 embeds (boxes) — a description holds only
4096 characters and the 49-enemy board is ~5000 (the links are ~60 characters each), so the last
section (Flying out) spilled into a second box. Now it is always **one embed**: the first ~4000
characters are the description, the rest continues in **fields of the same embed** (zero-width field
name), so the list runs on with no second box and no repeated header. Supersedes the "2 messages"
sizes quoted in the entries below.
- Limits kept: description <= 4000 (Discord 4096), each field <= 1000 (Discord 1024), whole embed
  content <= 5600 (Discord 6000 incl. title/footer). Measured, all one box with FULL lines and all
  enemies shown: 16/13/13/7 (available/hospital/abroad/flying) = 5089 of 6000; 0/0/49/0 (everyone
  abroad) = 5616. Beyond that, lines shorten, then the last rows are dropped with a note (as before).
- `/targets` uses the same board. 336 tests pass; ruff + mypy clean; three deliberate breaks (no
  description / field / total limit) each fail a test.
- **Not seen by eye in Discord:** how the small gap between the description and the field looks.

## Status: LIVE on Railway (deployed 2026-09-20, commit e1275e9) — abroad names on `#target-board` are clickable too

**Deploy verified 2026-09-20:** deployment `b9d8b6d9` SUCCESS; clean startup; `war.targets_published` for all 49
enemies. Not yet seen by eye in Discord: clicking an abroad name.

**First real flight caught (open check on the landing estimate):** an enemy took off Torn -> Canada on a
`light_aircraft` at 11:17:19 UTC (`war_players.status_since`). Prediction: 41 min x 0.66 = 27.1 min, so
landing ~**11:44:22 UTC**. To verify: find that player's first `player_status_history` row whose description
becomes "In Canada" and compare its `observed_at` (accurate to ~1 min). Record the error here.

Owner asked for abroad targets to click through to the attack page like available ones. Names in the
**Available** and **Abroad** sections now link to `page.php?sid=attack&user2ID=<id>` (`_CLICKABLE` in
`app/discord/target_board.py`); hospital and flying players stay plain text (they cannot be attacked).
Torn only lets you attack someone abroad if you are in the same country — the link just opens the page.
- **Size cost:** each link adds ~60 characters. Measured with 49 enemies: a war-shaped mix
  (16 available / 13 hospital / 13 abroad / 7 flying) is 5277 of Discord's 6000 characters and keeps full
  lines; with ~34+ enemies abroad at once the lines shorten (Est. BS / FF dropped) — nobody is ever dropped.
- 336 tests pass; ruff + mypy clean; two deliberate breaks (abroad not clickable / everyone clickable) each
  fail a test.

## Status: LIVE on Railway (deployed 2026-09-20, commit a9564c2) — attack-link fix + `#enemy-stats` as one list

- **Deploy verified 2026-09-20:** deployment `0d1443a5` SUCCESS; clean startup; `war.published` and
  `war.targets_published` for all 49 enemies right after start. A read-only query then showed take-off
  tracking working (16 real status changes recorded since the previous deploy) but **0 flight take-offs
  seen yet** — nobody had left in that window, so no landing estimate has been checked against a real
  flight. **Not yet seen by eye in Discord:** the one-list `#enemy-stats`, and clicking an Attack link.

- **Bug in the live target board (my mistake), fixed here:** the click-to-attack link used
  `loader.php?sid=attack&user2ID=…`, written from memory. Torn has retired `loader.php` (it
  answers `{"error": "This endpoint is no longer available. Please use the new endpoints
  instead (page.php)."}`). The real Attack button on a Torn profile page points at
  `https://www.torn.com/page.php?sid=attack&user2ID=<id>` (read from the page in Chrome, not
  assumed) — that is now the link. A test fails if `loader.php` comes back.
- **`#enemy-stats` is one list, not two:** rows were split at 35 per embed although a row is
  ~38 characters and one embed holds ~4000, so 49 enemies always became 2 embeds. Now packed by
  characters: 49 → 1 embed; a full 100-member faction (the Torn maximum) → 1 embed. Only a list
  that would overflow Discord's limit is split. Same change applies to `/roster`.
- 334 tests pass; ruff + mypy clean; three deliberate breaks (old split, no split guard, old link)
  each fail a test.

## Status: LIVE on Railway (deployed 2026-09-20, commit b1f7aa7) — `#target-board` is now ONE live list (available / hospital / flights)

Owner asked for one live list like the *Torn War Stuff Enhanced* userscript: who can be
attacked, who leaves hospital and when, who is flying where and when they land.

- **Board (`app/discord/target_board.py`, replaces `target_board_embed` in `embeds.py`):** one
  list in sections — **Available** (priority order, name is a click-to-attack link) → **Hospital /
  Jail** (soonest out first) → **Landing in Torn** (soonest first) → **Abroad** → **Flying out**.
  One line per player, not a code-block table, because Discord only counts down live
  (`<t:…:R>`, ticks every second in the reader's client) outside code blocks. Priority score/icons
  hidden (owner's choice); `#enemy-stats` untouched. `/targets` uses the same board.
- **Rules (`app/war/live_status.py`, pure):** hospital release = Torn's exact `status.until`.
- **Flight landing time is an ESTIMATE — Torn publishes none** (checked: `UserStatus` in Torn's
  OpenAPI has `until` = null for flyers, plus `plane_image_type` only). Estimate = when the bot
  first saw the flight begin + route minutes × plane factor; shown as `~`. Says "landing time
  unknown" when the take-off wasn't seen (already flying at first look, or a gap > 3 refreshes) —
  never a guess. Take-off tracking = new `war_players.status_description/status_since/
  status_checked_at`; plane type = new `player_status_history.plane_type` (all nullable columns,
  added automatically at startup; old code ignores them, so rollback is safe).
- **Evidence for the numbers (from 300+ real flights in `player_status_history`, 15–20 Sep):**
  the base minutes (Mexico 26 … South Africa 297) were recalled, not re-read (Torn's wiki blocks
  bots), but every route's median light-aircraft flight is 0.654–0.671 × them, so they hold.
  `light_aircraft` = 0.66 is confirmed (7 of 7 flyers seen had it). **`airliner` = 0.96 and
  `private_jet` = 0.285 are inferred by elimination** (slow cluster = only 2 Japan flights; fast
  cluster = 6 routes). Single flights stray ±4%, so an ETA can be a few minutes off.
  **TODO: after a day or two of recorded `plane_type`, confirm those two factors against real
  flights** (compare flight length per `plane_type`).
- 332 tests pass (51 new); ruff + mypy clean. Break-it checks: take-off trust after a gap, section
  order, the Discord size guard, "estimate without a seen take-off" and "attack link on hospital"
  each fail a test when disabled.
- **Deploy verified 2026-09-20:** deployment `c70582ed` SUCCESS; startup logged `database.column_added`
  for all 4 new columns; a read-only query then showed 49 `war_players` rows tracked, 0 with a seen
  take-off yet (correct — nobody had changed status since the deploy), `plane_type` being recorded
  (only `light_aircraft` seen so far). **The attack link in this deploy was broken — see the entry
  above.** Not yet seen by eye in Discord: the board's look and a real landing estimate.
- **After deploy:** enemies already in the air at that moment show "landing time unknown" until
  their next flight — the bot has to see a take-off first. Hospital times work immediately.
- **Limits:** one Discord message holds ~6000 characters. 49 enemies use ~4.5k. A much bigger
  roster shortens lines first, then drops the last rows with a "…and N more not shown" note.
- **Flag (not part of this change): `player_status_history` grows ~10 MB/day** (53 MB after 5
  days, 371k rows; whole DB 68 MB) because every enemy is saved every minute even when nothing
  changed. Saving only when a status changes would cut it sharply (not measured); worth doing
  before the database plan's size limit is close.

## Status: LIVE on Railway (deployed 2026-09-20, commit c7cea8c) — `#enemy-stats` no longer shows player status

Owner: the status shown per enemy in `#enemy-stats` was a snapshot from the last
refresh and read as out of date. Removed the `ST` column (OK/HOSP/JAIL/TRVL) from
`roster_embed()` in `app/discord/embeds.py`; the table is now `PLAYER / LVL / EST. BS / FF`.
- `roster_embed()` is shared, so `/roster` and the end-of-war scoreboard lose the column too.
- `#target-board` is untouched and still has its `ST` column. If that also reads stale,
  it is a separate change.
- Cause of the staleness was **not** investigated — the column was removed, not fixed.
  Note `refresh_active(include_stats=False)` (the cheap status pass) does re-publish
  `#enemy-stats`, so it is not simply "only updated on the slow pass".
- 281 tests pass; ruff + mypy clean. No test asserted the column.
- **Deploy verified 2026-09-20:** deployment `6fa7c2bc` SUCCESS; startup logs clean;
  `war.published players=49` logged twice right after start (the stats message was re-rendered
  with the new code). Not checked by eye in Discord — look at `#enemy-stats` to confirm.

## Status: LIVE on Railway (deployed 2026-09-19, commit 073ce09) — `RW: yes/no` in the war log + a paste-ready hits file for the payout sheet

The owner's admins pay members from a Google Sheet (`N&V Spreadsheet`, one tab per
war). Its raw-data block is columns A-G: `Attacker, Defender, Result, Respect,
#Chain, RW?, Timestamp` (newest hit first); a per-member block to the right
(`Total pay, RW, Attacks, Assists, Chain saver, Inside/Outside…`) plus a rates box
(attacks 100%, assists 75%, outside 50%, inside 300%, chain saver 150%…) do the maths.

- **War log:** each hit line now ends `RW: yes` / `RW: no` (no label on a legacy row until
  it is backfilled, rather than a wrong "no").
- **Files at war end:** the scoreboard message now carries **two** CSVs —
  `war-summary-…csv` (per player) and `war-hits-…csv` (every hit, war *and* outside,
  in the sheet's A-G layout, so admins paste it in and add the payouts).
- **What `RW?` means: a ranked war WEAPON was used** (owner's definition, twice
  corrected — see below). Torn's attack feeds never name the weapon, but the ranked-war
  weapon bonus **Warlord** shows up as `modifiers.warlord` (a respect multiplier: 1.0 =
  none; seen 1.15 / 1.17 / 1.18) and in `finishing_hit_effects`. So `RW? = warlord > 1`.
  Evidence it behaves like a weapon someone owns, from 1,682 real hits over 5 wars:
  present on 20% of hits, but only 6 of 37 players ever had it (LordZulu 115 of 122
  hits, MR_SPEAKER 89 of 166, The_eVagabond 0). **Limits, stated honestly:** we see the
  Warlord *bonus*, not the weapon — a ranked-war weapon without that bonus would read
  `no`. (Torn's `is_ranked_war` flag and `is_war_hit` are separate facts and are *not*
  RW; an interim version wrongly used `is_war_hit` because the owner's first answer to
  "what is RW?" was "ranked war hit" — corrected after the owner clarified.) The sheet's
  own `RW?` column could not be joined to Torn data (its tabs come from other wars), so
  this is validated against the owner's definition + the weapon-like pattern, not row by row.
- **`Timestamp`** = gap since the previous **chain** hit (chronologically), `m:ss`;
  `0:00` for the first chain hit and for non-chain hits (Lost/assist). Written the way
  the sheet holds it (the sheet's cells read `1:43` as `1:43:00`). Owner confirmed
  this meaning; the exact rule for which neighbour counts is my reading of the sheet —
  eyeball the first real file against a real tab.
- **New columns `war_hits.chain`** (Torn's chain number at the hit; 0 = not a chain hit)
  **and `war_hits.warlord`** (the Warlord multiplier; 1.0 = none).
  This is a change to a **live table**, and `create_all` never alters tables, so
  `Database.create_all` now runs `_add_missing_columns`: any nullable model column
  missing from an existing table is added with `ALTER TABLE … ADD COLUMN` (Postgres DDL
  checked: `… ADD COLUMN "chain" INTEGER` and `… ADD COLUMN "warlord" FLOAT`). NOT NULL
  additions are skipped and logged (`database.column_needs_migration`). No Alembic yet —
  this is the stop-gap. Rows stored before it get NULL in both.
- **Backfill:** `WarHitService.sync` re-reads from the oldest hit missing chain/warlord until they
  are filled (≤500 hits/tick), never re-posting anything (`posted_message_id` is
  untouched). If Torn stops returning a row it gives up after 3 fruitless passes
  (`war.hit_backfill_stalled`) so it can't pin every sync to an old window.
- Already-posted war-log lines are not edited, so only hits posted after the deploy
  carry `RW: …`; the end-of-war file covers every hit.
- 281 tests pass (27 new); ruff + mypy clean. Break-it checks: the warlord threshold,
  the backfill and the gap logic each fail their tests when disabled. Built from the live
  war's real data (362 hits): RW yes 70 / no 292 (19%, mostly LordZulu's hits).
- **Deploy verified 2026-09-19:** startup logged `database.column_added` for `war_hits.chain` and
  `war_hits.warlord`; a read-only query then showed 622 stored hits with 0 missing chain and 0
  missing warlord (backfill done), 91 `RW: yes`, 0 unposted; new war-log lines carry `RW: yes/no`.
  Not yet seen live: the war-end scoreboard with its two files (needs the war to end).
- **Not done:** the per-member half of the sheet (pay maths) — admins keep doing that
  in the sheet. Only the raw A-G paste is produced.

## Status: LIVE on Railway (deployed 2026-09-19, commit 3ed6758) — new wars reuse one category + channels (so a manual lock persists)

The owner wants War Information / Enemy Stats / Target Board / War Log bot-only.
The bot cannot set channel overrides (403 without Manage Roles, deliberately not
requested), so the owner locks the **category** by hand once (README §6) and the
bot now keeps using it:

- `WarChannelManager.ensure_workspace` first looks for this server's most recent
  earlier war that had a per-war category which still exists
  (`WarRepository.previous_workspaces`) and reuses that category **and** its four
  channels for the new war (each war still gets its own `discord_channels` rows).
  It renames the category to the current enemy (`GuildGateway.rename_category`,
  best-effort). Only if nothing reusable exists does it create fresh ones; a single
  deleted channel is recreated inside the reused category.
- Scoped by `guild_id`, so servers never share a workspace. Anchored on wars that
  *had* a category, so a server that used `/setup channel` can't have its one
  shared channel adopted as all four purposes.
- **Trade-off (decided, not asked):** the finished war's live boards (info, roster,
  targets) are left in place and the new war posts fresh ones below them — no
  deletion, so the last state of each old war stays visible. The war log is now
  one continuous log across wars. If the tidy-up is wanted, copy the old
  `live_message_id` onto the new war's rows so boards are edited in place instead.
- 253 tests pass (7 new in `test_war_channels.py`); ruff + mypy clean. Verified the
  reuse tests fail when the reuse is switched off. Not exercised against live
  Discord (it only triggers when the *next* war starts).
- The live war's category already exists in the DB, so the next war will reuse it.

## Status: LIVE on Railway (deployed 2026-09-19, commit 3ed6758) — scoreboard also attached as a downloadable spreadsheet

The end-of-war scoreboard message now carries a **CSV file** (`war-summary-<us>-vs-<enemy>-<date>.csv`)
— `Player, Player ID, Hits, Won, Draw, Lost, Assist, Other` — that opens directly in
Google Sheets (File -> Import -> Upload) or Excel.

- **Why a file and not a live Google Sheet:** a real Sheet needs a Google Cloud
  service account + Sheets/Drive API, and this bot is multi-tenant (each server
  is a different faction). A shared Google account would put other factions'
  data in the operator's Drive; per-server Google sign-in is a big feature. The
  file needs no credentials, works for every server, and "download it" is the
  stated goal. Revisit only if a *live, auto-updating* sheet is wanted.
- `Other` (interrupted etc.) is in the file, unlike the Discord table, so the
  columns always sum to Hits. UTF-8 BOM so Excel reads names correctly. Cells
  starting `= + - @` are prefixed with `'` (spreadsheet formula injection).
- Attachment plumbing: `GuildGateway.send_embeds(..., attachments=[(name, bytes)])`.
  If the server lacks **Attach Files** the gateway logs `gateway.attach_files_denied`
  and re-sends the embeds without the file (a missing Send Messages still fails
  loudly). The invite bitmask is now `117776` (was `85008`; the older link
  omits Attach Files). The owner's server already has it.
- **Read-only channels:** the owner wants War Information / Enemy Stats / Target
  Board / War Log to be bot-only. The bot **cannot** set channel overrides
  (verified 2026-09-19: PUT overwrite -> 403; needs Manage Roles, deliberately
  not requested). Workaround documented in README §6 — resolved by the entry
  above: the bot now reuses one category + channels, so the manual lock is
  done once, not per war. **Done on the live server 2026-09-19** and verified
  read-only via the Discord API: category `⚔️ WAR — …` has `@everyone` Send ✗ +
  `War Intel Bot` role Send ✓, and all four channels follow it (people can read,
  only the bot can post). Note for a new server: the role entry must be the
  green ✓, not the grey `/` (neutral) — neutral leaves the bot blocked.
- 246 tests pass (14 new: CSV content/formula safety/filename, attachment only
  on the scoreboard message, gateway fallback); ruff + mypy clean. CSV built from
  the live war's real data (13 players).

## Status: LIVE on Railway — new: end-of-war scoreboard in `#war-log`

When a war ends the log now posts, after the "war has ended" line, a
**scoreboard**: one row per player who made a war hit, with `HITS / W / D / L /
A` (won, draw, lost, assist), busiest first, plus a totals line. Green embed for
a win, red for a loss, grey otherwise.

- **Counting rules** (`app/war/hit_summary.py`, pure + tested): Torn `result`
  `Attacked`/`Hospitalized`/`Mugged`/`Arrested` = won, `Stalemate` = draw,
  `Lost` = lost, `Assist` = assist. Everything else (`Interrupted`, and any
  unseen result such as `Escape`/`Timeout`) counts in HITS but in no W/D/L/A
  column — deliberately not guessed, so the four columns may add up to less than
  HITS (the footer says so). Results seen live across 3 wars / 843 hits:
  Attacked, Hospitalized, Mugged, Lost, Stalemate, Assist, Interrupted.
- **Who is listed:** players with at least one *war* hit (outside hits and hits
  on us are excluded). Members who never hit are not listed — an easy add if
  wanted (join to the roster).
- **How it is queued:** `_reconcile` writes a `WAR_SUMMARY` log event right
  after `WAR_FINISHED`, only on the ACTIVE -> FINISHED transition (a war first
  seen already finished has no hit history, so gets none). The board renders the
  table from `war_hits` **at post time**, i.e. after the final hit sweep, and
  reuses the log event's `posted_message_id` for once-only posting.
  `unposted_for_war` now orders by `(occurred_at, id)` so the scoreboard cannot
  jump ahead of "war ended" on Postgres (ties in one transaction share a
  timestamp; SQLite tests can't prove this, so it is a guard, not a tested
  behaviour). `WarLogBoard` also gained a lock on event posting so a scoreboard
  can't be double-posted by overlapping jobs.
- **Risk to watch:** Torn's 100 req/min limit is *per person across all their
  keys/apps*, and the bot's own limiter cannot see other users of the same key
  (the Torn Banking bot, scripts). During development a probe hit error 5 once.
  The client retries with backoff and hit-sync failures are caught, so the worst
  case is a delayed hit batch — but if the user runs several bots on one key,
  watch for `TornAPIRateLimitError` in the logs.
- 232 tests pass (22 new in `tests/test_war_summary.py`); ruff + mypy clean.
  Rendered from the live war's real data (12 players, 243 hits; columns add up)
  with a throwaway DB — nothing posted to Discord.

## Status: LIVE on Railway — new: `#war-log` now lists every hit our faction makes

Requested from live use: the war log only said the war started. It now also
posts **every hit our faction makes**, colour-coded — **green** for war hits,
**orange** for outside hits (hits on anyone who is not the war enemy).

- **Data source:** Torn `GET /v2/faction/{id}/attacks` (verified live
  2026-09-19, works with the existing Limited Access key — see
  `docs/api-contracts.md`). `app/torn/attacks.py::AttackService` pages it
  (100/page, oldest first, `from` cursor, max 5 pages per tick).
- **What counts as a war hit — do not "simplify" this to Torn's flag.** Torn's
  `is_ranked_war` is only set on hits that *scored*. Measured on the live war:
  37 of 41 unflagged hits were losses / interrupts / assists **on the enemy**.
  So `war hit = is_ranked_war OR defender is in the enemy faction`
  (`app/war/hits.py::_to_row`). An earlier version used the flag alone and
  labelled ~90% of "outside" hits wrongly.
- **Storage:** new table `war_hits` (`WarHit`), one row per attack per war,
  unique on `(war_id, torn_attack_id)`. It is a **new table on purpose**:
  the schema is `create_all`-managed, so a new table appears on the live
  Supabase DB by itself, whereas a new column on an existing table would not.
  Hits made *on* us are stored too (`is_outgoing=False`, never posted) because
  the newest stored hit is the sync cursor.
- **Sync:** `WarHitService.sync` runs at the start of every `refresh_active`
  (1 extra Torn request/min normally). First run for a war starts at the war's
  own start time (whole war so far — the owner's choice); later runs resume 2
  minutes behind the newest stored hit (overlap, deduped by the unique key). A
  huge backlog is worked off over several ticks (≤500 fetched, ≤400 posted per
  tick). A final sweep runs when the war ends; a war first seen already
  finished is not back-filled.
- **Posting:** `WarLogBoard.publish_pending_hits` (called first by
  `DiscordWarPublisher.publish_log`, so "war has ended" never lands above the
  last hits). Consecutive hits of one kind share one coloured embed; several
  embeds share one message (Discord: ≤10 embeds, ≤6000 chars — enforced in
  `app/discord/hit_embeds.py`). Live war: 240 hits -> 4 messages.
- **Failure modes:** a key that cannot read attacks (the key owner's faction
  position lacks API access, or a lower key level) logs `war.hit_sync_failed`
  **once per war** and everything else keeps working — no hits appear in the
  log, nothing crashes. Discord offline: hits stay unposted and go out later.
- 210 tests pass (36 new: `test_attacks.py`, `test_hit_embeds.py`,
  `test_war_hits.py`); ruff + mypy clean. Verified end-to-end against the live
  war with a throwaway in-memory DB (nothing posted to Discord).
- **Not yet done / ideas:** hits *against* us (owner said no for now — the rows
  are already stored, so it is a small add); per-player hit counts / a
  leaderboard could be built from `war_hits`; the two older `_as_int` copies in
  `faction.py`/`warfare.py` could switch to the new `app/utils/parsing.as_int`.

## Status: LIVE on Railway — post-launch fix: `#target-board` now shows battle stats

From live use: `#target-board` listed rank/level/score/priority/status for
every enemy but never their estimated battle strength or fair fight —
`TargetView` (`app/war/target_service.py`) simply never carried those two
fields, even though the `RosterEntry` it's built from already has both.
Added `estimated_bs`/`fair_fight` to `TargetView` (defaulted to `None`,
matching `RosterEntry`'s own optional-field convention), populated from the
roster entry in `TargetingService.refresh` and, for the `/targets` command's
DB-read path, from a fresh `StatEstimate` lookup in `app.war.summary.
load_target_view` (the same latest-estimate query `load_roster_view` already
did — `load_target_view` just never had it). `target_board_embed` gained
`EST. BS` / `FF` columns matching `roster_embed`'s. New coverage: `tests/
test_summary.py` (new file — this read-path had no test coverage at all
before), plus regression tests in `test_embeds.py` and
`test_target_service.py`. 174 tests pass; ruff + mypy clean.

## Status: LIVE on Railway — post-launch fix: `#enemy-stats` / `#target-board` no longer truncate

From live use with a real 49-member roster: `roster_embed()` and
`target_board_embed()` (`app/discord/embeds.py`) silently cut every table off
at `_ROSTER_MAX_ROWS = 35` with a "showing top 35 of 49" note — a known,
documented shortcut from Phase 4 ("full split-across-messages rendering is a
later refinement"). Every enemy now always appears: both functions paginate
into multiple `discord.Embed`s (35 rows/page, growing the page size instead
of ever dropping a row if a roster were ever large enough to need more than
10 pages) instead of slicing the list. A Discord message can hold up to 10
embeds and 6000 combined characters, so this stays one live message per
purpose — no change to the create-once/edit-in-place model, no new
multi-message tracking (`discord_channels.extra_message_ids` — reserved for
that alternative since Phase 4 — is still unused).

Required threading `list[discord.Embed]` through the stack: `GuildGateway.
send_embed`/`edit_embed` → `send_embeds`/`edit_embeds` (list-only, so a
single embed is just `[embed]` — `WarLogBoard` updated to match),
`WarBoard.publish` now accepts `discord.Embed | list[discord.Embed]`, and
the `/enemies` + `/targets` slash-command replies pass `embeds=` instead of
`embed=`. New `tests/test_embeds.py` (6 cases, includes a regression test
reproducing the exact 49-row cutoff) plus a `WarBoard` case proving a
refresh that shrinks from 2 pages to 1 replaces the message's embeds rather
than leaving a stale second page. 170 tests pass; ruff + mypy clean.

## Status: LIVE on Railway — post-launch fix: merged `#enemy-roster` into `#enemy-stats`

From live use: `#enemy-stats` was a dead channel — reserved in
`CHANNEL_LAYOUT` since Phase 4 (`app/discord/war_channels.py`) with a
matching `wars.discord_stats_channel_id` column, but nothing ever published
to it. The per-player trend/confidence engine it was meant to surface
(`app/war/intelligence.py`) only ever shipped as the on-demand `/stats
<player>` command, never as an auto-updating aggregate embed. Meanwhile
`#enemy-roster` (the `roster` purpose) already carried everything actually
needed per war — name, level, est. BS, FF, status — and was the channel
actually being read.

Rather than build the unrequested aggregate-trend board, the `roster`
purpose was deleted and `roster_embed()` now publishes into the `stats`
purpose instead, so the existing content lives in `#enemy-stats` and there's
one channel instead of two. Removed: the `roster` entry from
`CHANNEL_LAYOUT`, `discord_roster_channel_id` (dropped from the `War` model
— schema is `create_all`-managed with no Alembic yet, per
`migrations/README.md`, so this is a no-op column removal, not a migration;
any already-deployed DB just keeps an unused orphan column). Per-war
category + channel count is now 4, not 5. 163 tests pass (`test_war_channels.py`
updated to match). `/stats <player>` is unaffected — separate code path,
untouched.

## Status: LIVE on Railway

The bot is deployed and running — Discord connected, global commands
synced, all three scheduler jobs (`war-detect`, `war-status-refresh`,
`war-stats-refresh`) ticking cleanly, zero crashes since deploy.

**Added post-launch, from live testing:** `/setup channel <#channel>` — the
user's test server wanted everything in one existing channel rather than the
bot's default auto-created category + 5 channels. `guild_configs` gained
`war_room_channel_id`; when set, `WarChannelManager.ensure_workspace()` maps
every purpose (information/roster/stats/targets/log) to that one channel
instead of creating anything (`WarChannelManager._ensure_single_channel_workspace`).
No changes needed to `WarBoard` or `WarLogBoard` — they already look up a
channel by `(war_id, purpose)` via the `discord_channels` table, never by
guessing a channel is unique to one purpose. Known limitation, not yet
addressed: in single-channel mode, a finished war's live messages are never
cleaned up, so a server that runs many wars will accumulate old
information/roster/targets messages in that one channel over time (each new
war just posts fresh ones below them). 163 tests pass.

- **Project:** `torn-war-intelligence-bot` (Railway workspace "zulubax's
  Projects", region `ams`, Hobby plan). One service, built directly from
  this repo's `Dockerfile` — no separate wrapper.
- **Fixed to get there:** the `Dockerfile`'s `RUN --mount=type=cache,
  target=/root/.cache/uv` layers failed on Railway's builder with
  `missing an id argument`, then (after adding one) `missing the cacheKey
  prefix from its id` — a Railway-specific requirement on cache-mount IDs
  that isn't documented plainly enough to guess correctly. Rather than
  reverse-engineer their exact ID format for what's purely a build-speed
  optimisation, the cache mounts were removed entirely; `uv sync` now runs
  without one. Slower cold builds, works on every builder.
- **Variables set on Railway** (never committed): `DISCORD_TOKEN`,
  `CONFIG_ENCRYPTION_KEY` (freshly generated this deploy — **back it up**,
  losing it means every server's saved `/setup` becomes unreadable),
  `DATABASE_URL` (Supabase Session Pooler, see below), `FFSCOUTER_API_KEY`
  (operator fallback).
- **Discord application already existed** (token was already in `.env` from
  an earlier session) — bot user `War Intel Bot#2115`, application/client ID
  `<BOT_CLIENT_ID>`. Invite link (scopes `bot` + `applications.commands`,
  the 5 permissions from `README.md` §6 = bitmask `85008`):
  `https://discord.com/api/oauth2/authorize?client_id=<BOT_CLIENT_ID>&permissions=85008&scope=bot%20applications.commands`
- **Fixed, in two passes:** the invite link first failed outright with *"This
  installation is not supported"* — the application's **Installation
  Contexts** (Developer Portal → app → Installation) had only `User Install`
  checked, not `Guild Install`. Enabling `Guild Install` (with its own
  **Default Install Settings**: scopes `bot` + `applications.commands`, the 5
  permissions) fixed the error, but the very next attempt installed the bot
  as a **personal app on the user's account** instead of adding it to a
  server — because `User Install` was *also* still enabled, and Discord let
  the flow default to it. This bot has no legitimate use as a per-user app
  (it's a multi-server guild bot by design, full stop), so `User Install` was
  unchecked entirely rather than just hoping the right tab gets picked next
  time — the app now only supports `Guild Install`, so the invite link can't
  land anywhere else. If a future Discord application (e.g. for another bot)
  hits either error, check this setting first.

**Next to verify live** (needs Discord-side action from the user): invite the
bot via the link above to a real test server, then run `/setup faction`,
`/setup torn-key` (+ optionally `/setup ffscouter-key`) in that server. Watch
`railway logs --lines 50 --latest` for `discord.guild_joined` on invite and
`multi_guild.manager_built` within a minute of `/setup` completing.

**Cloudflare Containers was tried first and abandoned.** Got as far as a
working `wrangler deploy --dry-run` and pushed secrets, but the real deploy
hit `Unauthorized: ... Deploying containers requires the Workers Paid plan`.
Priced out at ~$20-25/mo for this bot (Workers Paid $5 base + container
compute/memory/disk overage for an always-on container), because Cloudflare's
container model is request-driven with a sleep-on-idle timer, which this
bot's pure-outbound-only traffic pattern fights the whole way (needed an
`onActivityExpired()` override + a cron-trigger keep-alive just to stay up).
User decided to switch to **Railway** instead — the platform already proven
for the sibling Torn Banking bot, no sleep-on-idle model to fight. All
Cloudflare scaffolding (`wrangler.jsonc`, `worker/`, `package.json`,
`node_modules/`) removed; the Cloudflare Worker + its secrets that had been
created were deleted (`wrangler delete --name torn-war-intelligence-bot`).

**Database: Supabase project `torn-war-intelligence` (ref `ueotecutycgbamikplew`,
`eu-central-1`)** — org "ZuluBax's Org", free tier, Data API disabled on
purpose (the bot talks to Postgres directly via `asyncpg`, never needs
Supabase's REST layer, so there's no reason to expose one). Connection uses
the **Session Pooler** (`aws-0-eu-central-1.pooler.supabase.com:5432`, IPv4)
rather than the IPv6-by-default direct connection, since Railway's IPv6
egress wasn't a known quantity. The password contains URL-reserved characters
(`$ & # *`); it's percent-encoded before being written into `DATABASE_URL` —
see the one-off script pattern in shell history, or just re-encode with
`urllib.parse.quote(password, safe="")` if the string is ever rebuilt by
hand. The Railway account's Free plan couldn't host a second project
(blocked on *"Free plan resource provision limit exceeded"*); resolved by
the user upgrading to Hobby (~$5/mo, same tier the sibling Torn Banking bot
already pays for).

After deploy: invite the bot to a test server and run `/setup` for real —
this still hasn't been exercised live (see Phase 4.5 note below).

Added this session (kept regardless of hosting platform): `app/health.py`
(`HealthServer`, `GET /health`) — the bot otherwise has zero inbound HTTP
surface, useful for any platform's healthcheck — wired into `app/main.py`,
config'd via new `Settings.health_port` (default 8080), `tests/test_health.py`
(3 cases). Full suite: 112 pass. `Dockerfile` now `EXPOSE 8080`.

## Status: Phase 7 (war history) — complete

Turned out to be a small phase: finished-war detection (`WarState.
WAR_FINISHED`), result capture (`War.result`), and never deleting historic
roster/estimate rows were all already side effects of Phases 2-3's design —
there was nothing left to *build* for those, only to *surface*.

- **`app/war/history.py` `WarHistoryService.recent(guild_id, limit=10)`** —
  a thin, guild-scoped read wrapper over the already-existing
  `WarRepository.list_history` (finished wars only, most recent first). The
  original Phase 1 skeleton had a `finalise()` method and a non-guild-scoped
  `recent()`; both were corrected on delivery — `finalise()` was dead weight
  (the state transition it described already happens inline in
  `WarManager._reconcile`), and every other read in this multi-tenant
  codebase is guild-scoped, so `recent()` needed to be too.
- **`app/discord/embeds.py` `history_embed`** — a fixed-width table (date,
  opponent, WIN/LOSS/DRAW with a colour icon), reusing the roster/target
  embeds' truncation-table style. New `app.utils.time.london_date` helper for
  the date column (existing `london_clock` is time-only).
- **`/history`** wired for real, reading through the new
  `BotServices.war_history`.
- Tests: 158 pass. New: `test_war_history.py` (guild scoping, ordering,
  limit, empty case).

## Status: Phase 6 (targeting) — complete

`#target-board` now shows a ranked, disclaimed TARGET PRIORITY list instead of
the "coming soon" card, kept current by the same roster build/refresh cycle
Phase 5 already runs — no separate scheduler job needed.

- **`app/war/targeting.py` `rank()`:** pure, dependency-free scoring —
  min-max normalises estimated battle strength / Fair Fight / level *within
  the batch passed in* (a relative "easiest pick on this roster" ranking, not
  an absolute scale), weighted 0.60/0.25/0.15 by default (`TargetingWeights`,
  documented in `docs/targeting.md`). An unconfirmed value (`None`) never
  outranks a confirmed one. Current status is a **hard gate**, not a fourth
  weight: an unavailable enemy (hospital/jail/travelling/unknown) is always
  capped at `LOW` and always sorted after every attackable target, though its
  underlying score is preserved so it re-ranks correctly once available again.
- **`app/war/target_service.py` `TargetingService.refresh(war_id, roster)`:**
  turns a roster already built this cycle into `TargetInput`s, ranks them, and
  replaces (not appends to) the `target_rankings` snapshot for that war — a
  player on the roster but not yet in the `players` table is still returned
  in the view, just not persisted (nothing to key the FK on yet).
- **`WarManager`** gained an optional `targeting_service`; both `_build_roster`
  (new war) and `refresh_active` (Phase 5's ticks) call it after publishing
  the roster and before publishing the log, so `#target-board` and
  `#enemy-roster` never drift out of sync. A targeting failure is caught and
  logged, same as a roster or Discord failure — it never kills the tick.
- **`WarPublisher`** (the protocol `WarManager` depends on) gained
  `publish_targets`; `DiscordWarPublisher` renders it via the existing
  `WarBoard` (edit-in-place, reusing the "targets" channel Phase 4 already
  creates) and `app.discord.embeds.target_board_embed`.
- **`/targets`** reads the persisted snapshot (`load_target_view`, mirroring
  `load_roster_view`) rather than recomputing — recomputation only happens on
  the refresh cadence — and takes an `available_only` filter.
- Tests: 154 pass. New: `test_targeting.py` (algorithm), `test_target_service.py`
  (service + persistence), targeting-wiring cases in `test_war_manager.py`,
  a `publish_targets` case in `test_war_channels.py`.

Not yet done live: a real `/targets` and a real scheduled refresh against
Discord (unit-tested against fakes/stubs, same as every phase since 4).

## Status: Phase 5 (live updates) — complete

The Discord war workspace now stays current on its own instead of only
updating once at war registration.

- **Roster diffing:** `EnemyRosterService.refresh_with_diff(war_id,
  fetch_estimates=)` rebuilds the enemy roster and reports who joined/left the
  faction since the last refresh (`WarPlayer.left_faction`, cleared again if
  someone rejoins). `fetch_estimates=False` skips the FFScouter call entirely
  for a cheap, Torn-only pass — the roster still shows each player's last
  known estimate, and it is *not* treated as a FFScouter outage.
- **War-log events (`app/war/warlog.py`):** pure factories for
  `WarLogEvent` rows — `WAR_REGISTERED`, `STATE_CHANGE`, `WAR_FINISHED`
  (a state transition straight to "won"/"lost" gets the finished message, not
  a generic state-change one), `ROSTER_JOIN`, `ROSTER_LEFT`. `WarManager`
  writes these inside the same transaction as the change that caused them.
- **`WarLogBoard`** (`app/discord/war_channels.py`): posts each pending event
  **once** to `#war-log` — append-only, unlike `WarBoard`'s edit-in-place —
  and marks it posted. `DiscordWarPublisher.publish_log()` wires it in;
  `WarPublisher` (the protocol `WarManager` depends on) gained that method.
- **Two new scheduler cadences** (`app/main.py`, both via
  `MultiGuildWarRunner.refresh_tick`): `status_refresh_interval_seconds`
  (default 60s, Torn-only) and `stats_refresh_interval_seconds` (default
  900s, includes FFScouter) call `WarManager.refresh_active(include_stats=)`
  for every ready server's active war.
- **`/refresh`** is real: `MultiGuildWarRunner.manual_refresh(guild_id)`,
  rate-limited per server (cooldown = `status_refresh_interval_seconds`, so a
  manual refresh is never more expensive than the automatic cadence). The
  command now defers + follows up, since a real refresh can take longer than
  Discord's 3-second interaction window.
- **Found and fixed along the way:** `EnemyRosterService.refresh()` used
  `session.scalar()` on a multi-column `select()`, which silently returns
  only the first column, not a row — this raised on any real call and had
  never been exercised by a test. Also: SQLite (dev/tests) hands back naive
  datetimes for any `DateTime(timezone=True)` column regardless of the flag —
  every timestamp column now goes through a `UTCDateTime` type decorator
  (`app/database/models.py`) that coerces to UTC-aware on read; a no-op on
  Postgres (prod), which already round-trips tz-aware values correctly.
- Tests: 136 pass (`test_warlog.py` new; roster-diff cases in
  `test_roster.py`; log-event + `refresh_active` cases in
  `test_war_manager.py`; `WarLogBoard` cases in `test_war_channels.py`;
  `refresh_tick`/`manual_refresh` cases in `test_multi_guild.py`; a
  regression test for the timezone fix in `test_database.py`).

Not yet done live: a real `/refresh` and a real scheduled refresh tick against
Discord + Torn + FFScouter (unit-tested against fakes/stubs throughout, same
as every phase since 4).

## Status: Phase 4.5 (multi-server) — complete

The bot is now multi-tenant. It is invited to any Discord server via its OAuth2
link (no `DISCORD_GUILD_ID`); each server configures itself with `/setup`
(faction ID + its own Torn API key, stored encrypted). One `MultiGuildWarRunner`
tick iterates every configured server, each with its own `TornClient` /
rate-limit budget; FFScouter's per-IP limit is shared. Verified: the bot boots,
connects, syncs commands globally, and the runner tick is a no-op with no
servers configured. 109 tests pass.

Not yet done live: an actual `/setup` in an invited server (the operator's test
server isn't invited yet). Everything is unit-tested (`test_crypto`,
`test_guild_config`, `test_multi_guild`, updated `test_war_manager`).

### Phase 4.5 — key pieces
- `app/services/crypto.py` `SecretCipher` — Fernet; key from
  `CONFIG_ENCRYPTION_KEY` or an auto-generated git-ignored `config/secret.key`.
- `app/database/models.py` — `GuildConfig` (guild_id PK, encrypted key columns,
  `is_ready`); `wars` gains `guild_id`, unique on `(guild_id, torn_war_id)`.
- `app/config/guild_config.py` `GuildConfigService` — set_* / resolve /
  list_ready / describe; decrypts on read; per-server FFScouter key overrides
  the operator fallback.
- `app/discord/setup_commands.py` — the `/setup` group (admin-only, ephemeral,
  Torn key validated live before saving).
- `app/discord/bot.py` — global sync + instant per-guild sync on ready /
  `on_guild_join`; `discord_dev_guild_id` for instant local sync.
- `app/war/multi_guild.py` `MultiGuildWarRunner` — per-guild manager cache keyed
  by a config signature; drops managers for disabled servers; isolates a
  failing server from the rest.
- `TornClient` / `FFScouterClient` now take `api_key=` + `base_url=` directly
  (`.from_settings()` kept for local dev / the probe script).
- `WarManager` takes `guild_id`; `WarRepository` methods are guild-scoped.
- `app/discord/permissions.py` — intel-access role comes from `guild_configs`,
  not `.env`.
- `.env` now only needs `DISCORD_TOKEN` (+ ideally `CONFIG_ENCRYPTION_KEY`).

## Phase 4 (Discord war workspace) — complete

When a new, not-yet-finished war is registered the bot now (a) builds the enemy
roster (Phase 3), then (b) creates the Discord war workspace — a category plus
the five channels — **once**, and posts a live war-information embed and a live
enemy-roster embed that it *edits in place* on every refresh instead of
reposting. `/war`, `/war-status` and `/enemies` read the same data back from the
database.

The create-once / idempotency / edit-in-place / recreate-deleted logic is fully
unit-tested against a fake gateway; the real Discord run is pending the bot
token (`DISCORD_TOKEN` still blank in `.env`).

### Phase 4 — what works now
- `app/discord/gateway.py` — `GuildGateway` Protocol + `DiscordGuildGateway`
  (resolves the guild lazily; raises `GatewayNotReady` before the bot connects).
- `app/discord/war_channels.py` — `WarChannelManager.ensure_workspace(war_row_id)`
  (category on `wars.discord_category_id` + 5 `discord_channels` rows; recreates
  a channel the user deleted; idempotent) and `WarBoard.publish(war_id, purpose,
  embed)` (send first time → store `live_message_id`; edit thereafter; repost if
  the message was deleted).
- `app/discord/war_publish.py` — `DiscordWarPublisher` implements the
  `WarManager.WarPublisher` protocol; the manager stays Discord-import-free.
- `app/discord/embeds.py` — `war_info_embed`, `roster_embed` (fixed-width table,
  top 35, "showing top N of M", FFScouter-unavailable note).
- `app/war/summary.py` — rebuilds a war summary / `Roster` view from the DB for
  the slash commands.
- `app/discord/commands.py` — `/war`, `/war-status`, `/enemies` implemented.
- `main.py` wires the gateway + channel manager + board + publisher into
  `WarManager` (built after the bot so the gateway has its reference).
- Tests: 95 pass. New: `tests/test_war_channels.py` (+ `tests/fake_gateway.py`),
  publisher cases in `test_war_manager.py`. pytest now tolerates discord.py's
  `audioop` DeprecationWarning at import.

## Phase 3 (FFScouter + enemy roster) — complete

### Phase 3 — what works now
- `app/war/roster.py` `EnemyRosterService.build(war_id, enemy_faction_id)` —
  Torn `get_members` → upsert `Player` + `WarPlayer` + `player_status_history`;
  FFScouter `get_stats` → append one `StatEstimate` per player that has an
  estimate; assembles a `Roster` (sortable by estimated strength). `refresh()`
  resolves the enemy id from the `War` row and rebuilds. Idempotent for
  players/links; estimates are append-only history.
- FFScouter-down handling (spec §27): `estimates_available=False`, roster still
  built, each entry falls back to that player's last stored estimate, nothing
  deleted.
- `app/war/intelligence.py` `summarise()` (pure) + `IntelligenceService` —
  current/previous estimate, % change, ▲/▼/▬ arrow, age label, and a
  HIGH/MEDIUM/LOW/UNKNOWN confidence from the documented, configurable rubric.
- `app/war/lookup.py` `resolve_player()` — Torn ID or case-insensitive name → row.
- `app/discord/commands.py` — `/enemy <player>` and `/stats <player>` implemented
  (read from the DB via `bot.services`); the rest still return the "later phase"
  card. `app/discord/embeds.py` `player_intel_embed()`.
- `app/discord/services.py` `BotServices` container; `main.py` builds
  `FFScouterClient`, `FactionService`, `EnemyRosterService`, `IntelligenceService`,
  wires the roster service into `WarManager`, and closes the FFScouter client on
  shutdown.
- Tests: 86 pass. New: `test_intelligence.py`, roster-service cases in
  `test_roster.py`.

## Phase 2 (Torn integration) — complete

### Phase 2 — what works now
- `app/torn/faction.py` `FactionService` — `get_basic`, `get_members`,
  `get_faction`; parses the real v2 member shape (id, level, days_in_faction,
  `last_action.timestamp`, `status.state`). Works for our own **and** an
  arbitrary enemy faction id.
- `app/torn/players.py` `PlayerService` — `get_profile` / `get_status` /
  `get_profiles` (fan-out, one bad id doesn't sink the batch).
- `app/torn/warfare.py` `WarfareService` — `list_ranked_wars`,
  `get_current_ranked_war` (newest entry from `/rankedwars`),
  `has_live_ranked_war` (secondary `wars.ranked` signal).
- `app/war/detector.py` `WarDetector.detect()` → `WarAssessment`
  (NO_WAR / WAR_DETECTED / WAR_ACTIVE / WAR_FINISHED via the pure `assess()`).
- `app/war/manager.py` `WarManager.tick()` — reconciles the `wars` table:
  registers a new war once (unique `torn_war_id`), upserts both faction rows,
  applies state / result / end-time changes, records WIN/LOSS from `winner`.
  A war first seen already-finished goes straight to history.
- `app/main.py` schedules `WarManager.tick` every `WAR_CHECK_INTERVAL_SECONDS`
  (skipped when `MOCK_API=true`). `TornClient` is closed on shutdown.
- Tests: 74 pass. New: `test_torn_faction`, `test_torn_warfare`,
  `test_torn_players`, `test_war_manager`, `WarDetector` cases in
  `test_war_detector`. Fixtures captured from the live API in
  `tests/torn_fixtures.py`.

## Phase 1 (Foundation) — complete

## What works now
- Config load + validation (`app/config/settings.py`), secrets as `SecretStr`.
- Structured logging with secret redaction (`app/utils/logging.py`).
- Async DB layer, full ORM schema, repositories (`app/database/`).
- Client-side rate limiter, TTL cache, scheduler wrapper (`app/services/`).
- Async Torn v2 client — auth, retry, error mapping (`app/torn/client.py`).
- Async FFScouter v1 client — batching, rate limit, error mapping,
  response parsing (`app/ffscouter/client.py`). Contract verified 2026-09-10.
- Discord bot that connects and registers all 9 slash commands (skeleton
  replies) with the permission model in place.
- `python -m app.main` boots: config → logging → DB schema → scheduler → bot.
- Test suite (unit, APIs mocked), Dockerfile, docker-compose, docs.

## Architecture decisions
See `docs/architecture.md`. Key ADR-level choices:
- **ADR-1:** `uv` + Python 3.12 toolchain, not the system Python (Windows Store
  alias was unusable; uv gives reproducible cross-platform installs).
- **ADR-2:** SQLAlchemy async with SQLite for dev / Postgres for prod, one ORM.
- **ADR-3:** One HTTP client + one rate limiter per upstream API; never shared.
- **ADR-4:** No Redis in the MVP — single instance, in-process cache (spec §35).
- **ADR-5:** FFScouter client built to a verified contract in Phase 1 (not
  deferred to Phase 3) because the docs were confirmable now; only DB
  persistence of estimates is left for Phase 3.
- **ADR-6:** `stat_estimates` append-only; `confirmed_stats` a separate table.
- **ADR-7:** All `DateTime` columns go through `app.database.models.UTCDateTime`,
  never raw `DateTime(timezone=True)` — SQLite (dev/tests) silently drops
  timezone info on read regardless of the flag; Postgres (prod) is unaffected,
  so this is a dev-only correctness fix with no prod behaviour change.

## Open decisions
1. **LICENSE** — not chosen. `LICENSE` is a placeholder. Pick one before any
   public repo / release. (User decision.)
2. ~~Torn key access level~~ — **RESOLVED 2026-09-10.** The user's key reads our
   own + an arbitrary enemy faction's `basic` / `members` / `rankedwars` and any
   player `profile`. That is a **Limited Access** (or equivalent Custom) key
   from a faction member. Recorded in `docs/api-contracts.md`; README §3 still
   to be tightened.
3. **Pending-war visibility** — still open; needs a live war to observe. Does a
   matchmade-but-not-started war appear in `/rankedwars` before `start`?
   Detection currently keys off the newest `/rankedwars` entry, which is
   correct either way; this only affects how early the alert fires.
4. **Hosting target** — any small container host works; cost expected well
   under $20/mo. No decision needed unless the user has a preferred platform.

## Known issues / tech debt
- Alembic not yet initialised — schema is created from ORM metadata on startup.
  Fine for MVP; cut the first migration before the schema stabilises in prod.
- `MOCK_API=true` currently just disables the war-detection job — there are no
  mock fixtures for a full offline run yet (spec §39). Deferred.
- The `#enemy-stats` channel content is still a skeleton (no phase has scoped it yet).
- Live runs not yet exercised: a real `/setup` in an invited server, the real
  FFScouter call, and the real Discord workspace creation. All unit-tested
  against fakes. The operator's test server still needs to be invited.
- No licensing layer (deliberate — user decided against it for now).
- Old ADR-3 ("one HTTP client per upstream") now means **one per server** for
  Torn; a single shared limiter for FFScouter (per-IP limit).
- `roster_embed` never cuts rows off: it is one list (about 100 rows fit in one embed) and
  splits across embeds in the same message only if a list would overflow Discord's limit.
- `requirements.txt` is generated from `uv.lock`; regenerate it whenever
  dependencies change (`uv export --no-dev --no-hashes --format requirements-txt`).

## Next step
Deploy Phase 8 after the war (see the top entry), then live-check `/spy add` with a real
spy paste and `/matchups`. Left from Phase 8: the in-house estimator design note.

With Phases 1-7 all complete, the MVP's core loop — detect a war, build and
keep the enemy roster current, rank targets, log what happened, and look back
at history — is functionally done. What's left before this is genuinely
"done" is exercising all of it against live Discord/Torn/FFScouter (the
operator's test server still isn't invited) and clearing the Railway
deployment blocker above.
