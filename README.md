# Wrocław events bot (Telegram)

Collect upcoming events in Wrocław from ~30 **HTTP-only** sources (no login/captcha), dedupe, store in SQLite, and post new items to a Telegram channel.

## How it runs

- Intended schedule: **every 2 hours at :05**, **06:05–22:05 Europe/Warsaw** (cron on the VM; the bot only *does work* between **06:00 and 22:59** local — same window as `TIMEZONE` / `Europe/Warsaw`).
- Each run:
  - fetches sources listed in [`config/sources_phase1.yaml`](config/sources_phase1.yaml)
  - parses list pages (and optional detail pages where enabled)
  - normalizes + dedupes
  - posts only truly-new events
  - records per-source health (timeouts, 403/429 blocks, parse errors, empty results)

## Local run

Create a venv and install deps:

```bash
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Create `.env` (example):

```bash
TELEGRAM_BOT_TOKEN=123:abc
TELEGRAM_CHANNEL_ID=-1001234567890
ADMIN_TELEGRAM_ID=12345678
DB_PATH=./data/events.db
TIMEZONE=Europe/Warsaw
DRY_RUN=1
POST_MODE=digest
MAX_POSTS_PER_RUN=30
```

Run:

```bash
.\.venv\Scripts\python -m src.main
```

## Deploy to your GCP VM (GitHub Actions → SSH)

This repo includes a workflow: [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml).

### One-time GitHub repo setup (local → GitHub)

1. Create a new GitHub repository (public or private).
2. Add it as `origin` and push:

```bash
git remote add origin <YOUR_GITHUB_REPO_SSH_URL>
git push -u origin main
```

### One-time manual VM steps

1. Install prerequisites:
   - `python3`, `python3-venv`, `git`, `cron`
2. Create the app dir:
   - `/opt/wroclaw-events-bot`
3. Create `/opt/wroclaw-events-bot/.env`:
   - `TELEGRAM_BOT_TOKEN=...`
   - `TELEGRAM_CHANNEL_ID=...`
   - optional `ADMIN_TELEGRAM_ID=...`
   - optional `DB_PATH=/opt/wroclaw-events-bot/data/events.db`
   - optional `TIMEZONE=Europe/Warsaw`
   - optional `POST_MODE=digest` (or `immediate`)
   - optional `MAX_POSTS_PER_RUN=30`
4. Ensure timezone is correct (recommended):
   - set VM timezone to `Europe/Warsaw` **or** use `CRON_TZ=Europe/Warsaw` in crontab
5. Add cron entry — **every 2 hours at minute 5**, only in the hours the bot is allowed to run (**06–22** local, via `CRON_TZ=Europe/Warsaw` or a correctly zoned VM):

```cron
CRON_TZ=Europe/Warsaw
5 6-22/2 * * * cd /opt/wroclaw-events-bot && /opt/wroclaw-events-bot/.venv/bin/python -m src.main >> /opt/wroclaw-events-bot/data/cron.log 2>&1
```

   - Fields are: `minute hour day month weekday` → here: at **:05**, on hours **6, 8, 10, …, 22** (step of 2 within 6–22).
   - Optional: `5 * * * *` runs at **:05 every hour**; runs outside **06:00–22:59** still start the process but exit immediately (see `src/main.py` `_within_allowed_hours`).

### GitHub Actions secrets required

Add these secrets in your GitHub repo settings:

- `VM_HOST` (e.g. `1.2.3.4`)
- `VM_USER` (e.g. `tal`)
- `VM_SSH_KEY` (private key for SSH deploy)
- Optional: `VM_SSH_PORT` (default 22)

## Source health

After each run, the bot writes per-source status into SQLite and prints a short summary to stdout.
If `ADMIN_TELEGRAM_ID` is set, repeated failures (e.g. consecutive blocks) can trigger an admin ping.

## Posting modes

- `POST_MODE=immediate`: posts each newly discovered event (up to `MAX_POSTS_PER_RUN`).
- `POST_MODE=digest`: posts one message per run containing up to `MAX_POSTS_PER_RUN` new events.

