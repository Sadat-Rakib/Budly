# Set up StudyBuddy

~30 minutes. No coding. Your own free accounts. $0/month.

You need: Canvas at `https://canvas.ualberta.ca`, Telegram, Vercel, Supabase, cron-job.org, (optional) Slack.

## 1. Canvas

1. Open Canvas → Courses → All Courses → copy **Term** exactly (e.g. `Fall Term 2026`).
2. Account → Settings → Approved Integrations → New Access Token → Purpose `StudyBuddy`, expiry end of term → copy token once.

## 2. Telegram

1. `@BotFather` → `/newbot` → copy token.
2. Open your new bot → tap Start.
3. Wizard auto-detects chat ID via getUpdates (no @userinfobot needed).

## 3. Supabase

1. New project (region us-east-1 to match Vercel iad1). Don't reuse another product's.
2. Connect → **Session pooler** (port 5432, `*.pooler.supabase.com`). Copy URL, percent-encode password specials.
3. Locally: `uv run alembic upgrade head` then `uv run canvasbuddy sync` to seed.

Your project ref (already created): `stvweblwadvpnbgbpniz` → `https://stvweblwadvpnbgbpniz.supabase.co`.

## 4. Run the wizard

```powershell
uv sync
uv run canvasbuddy setup
```

It validates Canvas (`/users/self` + term courses), Telegram `getMe` + chat-ID detect, Slack test post, DB connect, timezone + slots, generates `WEBHOOK_SECRET`/`CRON_SECRET`, writes `.env` + `vercel.env` (0600), runs migrations + first sync.

Check anytime: `uv run canvasbuddy setup --check` / `uv run canvasbuddy doctor`.

## 5. Deploy bot (Vercel project #1, repo root)

```powershell
npm i -g vercel
vercel login
vercel link
# import vars from vercel.env: CANVAS_BASE_URL, CANVAS_TOKEN, CANVAS_TERM, TELEGRAM_BOT_TOKEN,
# TELEGRAM_CHAT_ID, USER_TIMEZONE=America/Edmonton, USER_NAME=Mir, DATABASE_URL,
# WEBHOOK_SECRET, CRON_SECRET, DIGEST_SLOT, NUDGE_SLOT, REVIEW_SLOT, CHECKIN_SLOT,
# NOTIFY_GRACE_MINUTES, SLACK_WEBHOOK_URL
vercel --prod
```

Region: iad1. If production 401, disable Deployment Protection for production.
Webhook:

```powershell
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" -d "url=https://<project>.vercel.app/api/telegram" -d "secret_token=<WEBHOOK_SECRET>" -d "drop_pending_updates=true"
```

Never run polling (`serve`) afterwards.

## 6. Slack (optional)

api.slack.com/apps → Create → From scratch → Incoming Webhooks ON → Add New Webhook → pick channel/DM → paste into `SLACK_WEBHOOK_URL`.

## 7. cron-job.org

Free account → Create cronjob: URL `https://<project>.vercel.app/api/cron`, every 15 min all days, GET, header `Authorization: Bearer <CRON_SECRET>`, failure notifications on.

Verify: `GET /api/cron?dry_run=1&now=2026-09-26T08:00:00-06:00` with same header returns `due:["review"]` + rendered payloads, sends nothing.

## 8. Landing page (Vercel project #2)

New Project → same repo → Root Directory `site`, enable include-outside-root, no env vars. Build runs `python3 ../scripts/build_release.py --out dist`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot silent | TELEGRAM_CHAT_ID mismatch; tap Start on bot; check webhook secret_token |
| `/week` empty | CANVAS_TERM exact mismatch |
| Token rejected | New Canvas token (password change/expiry) → rotate in Vercel + `.env` |
| Digest twice | Check `digests` unique (local_date,channel,kind); concurrent ticks → one wins |
| Slack 429/5xx | Retried with Retry-After; check webhook URL single-channel |
| Build fails secrets | Remove real tokens from tree; bundle scanner blocks webhooks/keys/DB URLs |
