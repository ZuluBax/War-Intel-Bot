# Deployment

The bot is a long-running process. It must stay running for the bot to be
online in Discord and respond to commands. Two ways to run it, pick one:

- **[Option A — Railway](#option-a--railway)** — no hardware to maintain,
  same platform and pattern already used for the sibling Torn Banking bot.
  Currently deployed this way.
- **[Option B — self-hosted](#option-b--self-hosted-home-server--vps)** — a
  Docker Compose stack (bot + PostgreSQL) on your own machine, e.g. the
  Raspberry Pi 5.

Cloudflare Containers was tried first and abandoned: it needs the Workers
Paid plan ($5/mo base) *plus* container compute overage that worked out to
roughly $20-25/mo for this bot, because Cloudflare's container model is
built around request-driven workloads that sleep when idle — this bot is a
pure outbound Discord-gateway client with no inbound traffic to keep it
awake, which fought that model the whole way (needing a cron-trigger
keep-alive hack) before the cost made it moot anyway. Railway has no such
sleep model and is already proven for this exact shape of app.

---

## Option A — Railway

Same `Dockerfile` as Option B — Railway builds and runs it directly, no
separate wrapper needed (unlike the abandoned Cloudflare approach).

The database is hosted Postgres on **Supabase** (free tier), not a Railway
Postgres plugin — already provisioned (project `torn-war-intelligence`,
`eu-central-1`), so there's no separate DB service to add on Railway.

### One-time setup

Prerequisites: Railway CLI installed and logged in (`railway whoami` to
confirm — same account as Torn Banking).

```bash
railway init                      # creates a new Railway project, or:
railway link                      # links this folder to an existing project
```

### Secrets

Set as Railway service variables (never committed to git):

| Variable | Required | Source |
|---|---|---|
| `DISCORD_TOKEN` | Yes | Discord Developer Portal → Bot → Reset Token |
| `CONFIG_ENCRYPTION_KEY` | Yes | Generate once — **back this up in a password manager**, losing it means every server redoes `/setup` |
| `DATABASE_URL` | Yes | `postgresql+asyncpg://postgres.<ref>:PASSWORD@aws-0-eu-central-1.pooler.supabase.com:5432/postgres` — the Session Pooler connection string from Supabase's project settings (IPv4, suits a persistent connection) |
| `FFSCOUTER_API_KEY` | Optional | Fallback key for servers that haven't set their own |

```bash
railway variable set DISCORD_TOKEN --stdin
railway variable set CONFIG_ENCRYPTION_KEY --stdin   # paste output of the command below
railway variable set DATABASE_URL --stdin
railway variable set FFSCOUTER_API_KEY --stdin       # optional
```

Generate `CONFIG_ENCRYPTION_KEY` (same command as Option B):

```bash
docker run --rm python:3.12-slim sh -c \
  "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

### Deploy

```bash
railway up -y --detach --json     # deploy the current folder
railway logs --lines 50 --latest  # check it came up clean
```

You want to see, in order: `database.schema_ready`, `scheduler.started`,
`discord.ready`, `discord.global_commands_synced`.

No public domain is needed — this bot has no inbound HTTP surface Discord
or users need to reach (the `/health` endpoint from `app/health.py` is only
for Railway's own healthcheck, not exposed).

### Operating

```bash
railway logs --lines 50 --latest    # check recent logs
railway restart --yes --json        # restart without rebuilding
railway up -y --detach --json       # ship a code change
railway variable set KEY --stdin --skip-deploys   # rotate a secret without an immediate redeploy
```

### Backups

The database is Supabase-hosted — use Supabase's own backup / point-in-time
restore (Database → Backups in its dashboard) rather than `pg_dump` over
Railway. `CONFIG_ENCRYPTION_KEY` only exists in Railway's variables and your
own backup of it — losing both makes every server's saved `/setup` keys
unreadable.

---

## Option B — self-hosted (home server / VPS)

This guide runs it with Docker
Compose (bot + PostgreSQL), which restarts automatically on crash and on host
reboot.

## Prerequisites on the host

- 64-bit Linux (x86-64 or arm64 — a Raspberry Pi 5 on 64-bit Raspberry Pi OS is
  fine; check with `uname -m` → `aarch64`).
- **Docker Engine** + the **Docker Compose plugin**
  (`docker compose version` should work).
- ~1 GB free disk, an always-on internet connection.
- The machine stays powered on. If it sleeps or shuts down, the bot goes offline.

### Installing Docker (Raspberry Pi OS / Debian / Ubuntu)

```bash
uname -m                              # must print: aarch64
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker                         # or log out and back in
docker compose version                # confirm it works
```

## 1. Get the code onto the host

From your dev machine (adjust the IP / username for your Pi):

```bash
scp -r "<path-to>/torn-war-intelligence" pi@<PI_IP>:~/
```

Then on the Pi: `cd ~/torn-war-intelligence`. Everything below runs from there.

## 2. Create `.env`

```bash
cp .env.example .env
```

Edit `.env` and set:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Discord Developer Portal. **Required.** |
| `CONFIG_ENCRYPTION_KEY` | Generate one and paste it in — see below. **Strongly recommended.** |
| `POSTGRES_PASSWORD` | Any long random string (used only between the two containers). |
| `FFSCOUTER_API_KEY` | *(optional)* A fallback FFScouter key for servers that don't set their own. |

Generate the encryption key:

```bash
docker run --rm python:3.12-slim sh -c \
  "pip install -q cryptography && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

Put the output in `.env` as `CONFIG_ENCRYPTION_KEY=...`.

> If you leave `CONFIG_ENCRYPTION_KEY` blank the bot generates one inside the
> `bot_config` volume on first run. That works, but **back up that volume** —
> losing the key means every server has to re-run `/setup`. Setting it in `.env`
> is simpler and safer.

`.env` is git-ignored and is never copied into the Docker image.

## 3. Build and start

```bash
docker compose up -d --build
```

First build takes a few minutes. Then:

```bash
docker compose logs -f bot
```

You want to see, in order:

```
database.schema_ready
scheduler.started
discord.ready            bot_user='...'   guilds=N
discord.global_commands_synced
```

`guilds=0` just means the bot isn't in any server yet — that's fine.

## 4. Invite the bot and configure a server

1. Open your OAuth2 URL Generator invite link (`bot` + `applications.commands`
   scopes, the 5 permissions from `README.md` §6). Authorise it into your
   server. The logs should show `discord.guild_joined` and
   `discord.guild_commands_synced`.
2. In that server, a member with **Manage Server** runs:
   - `/setup faction <your Torn faction ID>`
   - `/setup torn-key <your Torn API key>`
   - *(optional)* `/setup ffscouter-key <key>`
3. Within one `WAR_CHECK_INTERVAL_SECONDS` (60s) the logs show
   `multi_guild.manager_built` for that server, and war detection is live.

## 5. Everyday operations

```bash
docker compose logs -f bot            # follow logs
docker compose restart bot            # restart without rebuilding
docker compose up -d --build          # deploy a code change
docker compose down                   # stop everything (data volumes kept)
docker compose pull && docker compose up -d   # (if you switch to a prebuilt image)
```

## 6. Backups

Two things matter:

- **`pg_data` volume** — all configuration, wars, players, estimates.
- **`CONFIG_ENCRYPTION_KEY`** — from `.env` (or the `bot_config` volume if you
  didn't set it). Without it the encrypted Torn keys in the database are
  unreadable.

Back up the database regularly:

```bash
docker compose exec -T postgres pg_dump -U torn torn_war_intel > backup-$(date +%F).sql
```

Restore:

```bash
cat backup-YYYY-MM-DD.sql | docker compose exec -T postgres psql -U torn -d torn_war_intel
```

## 7. Updating

Pull the new code onto the host, then:

```bash
docker compose up -d --build
```

The schema is currently created from the ORM on startup (new tables/columns are
added automatically; destructive changes are not). Once Alembic migrations exist
(see `migrations/`), run `docker compose exec bot alembic upgrade head` instead.

## Option C — Raspberry Pi, keeping the Supabase database (current production)

Since 10 Nov 2026 the bot runs on a Raspberry Pi 5 (64-bit Raspberry Pi OS)
instead of Railway (Railway's free plan ran out of usage and stopped it).
Same Docker image as A/B, but `docker-compose.pi.yml` runs **only the bot** and
uses the existing Supabase `DATABASE_URL`, so every server's `/setup`, war
history and spy reports carry over. Install Docker as in Option B first.

**Never run two copies at once** (Pi + Railway, or two Pis): both would post
every board and log message. Railway's deployments are all removed; leave them so.

### `.env` on the Pi

Same four secrets as Railway. `CONFIG_ENCRYPTION_KEY` **must** be the original
key or the bot refuses to start (`crypto.key_mismatch`). To build it from
Railway without printing any value (dev machine, in the repo folder):

```bash
railway variable list --kv | grep -E '^(DISCORD_TOKEN|CONFIG_ENCRYPTION_KEY|DATABASE_URL|FFSCOUTER_API_KEY)=' > .env.pi
```

`.env.pi` is git-ignored. Keep a copy of `CONFIG_ENCRYPTION_KEY` in a password
manager: once Railway is gone, the Pi's `.env` is the only other place it lives.

### Copy the code and secrets, then start

From the dev machine (only git-tracked files are sent — not `.venv`, `.env`,
or the local database):

```bash
git archive main | ssh <user>@<pi> "mkdir -p ~/torn-war-intelligence && tar -x -C ~/torn-war-intelligence"
scp .env.pi <user>@<pi>:~/torn-war-intelligence/.env
ssh <user>@<pi> "chmod 600 ~/torn-war-intelligence/.env"
```

On the Pi:

```bash
cd ~/torn-war-intelligence
docker compose -f docker-compose.pi.yml up -d --build
docker compose -f docker-compose.pi.yml logs -f bot
```

Expect `database.schema_ready`, `scheduler.started`, `discord.ready`
(with your real `guilds=N`), `discord.global_commands_synced`, and **no**
`crypto.key_mismatch`.

### Updating

Re-run the `git archive ... | ssh ...` line (it overwrites the code, not
`.env`), then on the Pi `docker compose -f docker-compose.pi.yml up -d --build`.

### Notes

- Restarts by itself after a crash or a reboot (`restart: unless-stopped`);
  Docker starts on boot by default on Raspberry Pi OS.
- Container logs are capped at 3 × 10 MB so they can't fill the SD card.
- Supabase's free tier pauses a project after ~a week of no activity. While
  the bot runs it is never idle; after a long outage, restore it from the
  Supabase dashboard first or startup fails to connect.
- Backups: the data is still in Supabase (see Option A → Backups).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot offline in Discord | `docker compose ps` — is `bot` running? `docker compose logs bot` for why it exited. |
| `Cannot start - these must be set` | `DISCORD_TOKEN` missing from `.env`. |
| `crypto.key_generated` warning every restart | `CONFIG_ENCRYPTION_KEY` not set and the `bot_config` volume isn't persisting. Set the key in `.env`. |
| Bot exits at startup with `crypto.key_mismatch` | The encryption key isn't the one the saved API keys were made with (changed, or unset so a new one was generated). Put the original `CONFIG_ENCRYPTION_KEY` back (on Railway: check the variable). **Only if it's lost for good:** start once with `RESET_UNREADABLE_KEYS=true`, remove it again, and have every server re-run `/setup torn-key`. |
| `crypto.some_keys_unreadable` | Only the listed servers' saved keys are unreadable (e.g. a corrupt row); everyone else runs. Those servers re-run `/setup torn-key`. |
| Slash commands missing in a server | Re-invite the bot (commands sync on join). A 403 in the logs = missing `applications.commands` scope. |
| Build fails on `cryptography` | You're on 32-bit ARM. Uncomment the `apt-get install gcc libffi-dev` line in `Dockerfile`. |
| `TornAPIRateLimitError` for one server | That server's Torn key is shared with other scripts. Each server needs its own key. |
