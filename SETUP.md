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

## 5. Deploy bot + landing page (one Vercel project, repo root)

```powershell
npm i -g vercel
vercel login
vercel link
# Add vars from vercel.env (Settings → Environment Variables → add each as Sensitive,
# with the REAL value — a name with no value breaks the app): CANVAS_BASE_URL,
# CANVAS_TOKEN, CANVAS_TERM, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
# USER_TIMEZONE=America/Edmonton, USER_NAME, DATABASE_URL, WEBHOOK_SECRET,
# CRON_SECRET, DIGEST_SLOT, NUDGE_SLOT, REVIEW_SLOT, CHECKIN_SLOT,
# NOTIFY_GRACE_MINUTES, SLACK_WEBHOOK_URL, DASHBOARD_PASSWORD, APP_SECRET,
# and optionally OPENROUTER_API_KEY
vercel --prod
```

The same deployment serves the landing page (from `public/`) at `/` and the API at
`/api/*`. Regenerate `public/` after a version bump: `python scripts/build_release.py --out public`.

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

## 8. Landing page + dashboard

No second project needed — the bot deployment serves everything. `/` returns the
dashboard (Postbot + chat + landing cards), `/api/*` the API,
`/downloads/studybuddy-latest.zip` the self-host bundle, all from the committed
`public/` directory. Regenerate it after a version bump:
`python scripts/build_release.py --out public`.

The dashboard chat stays locked until `DASHBOARD_PASSWORD` is set on the deployment.
`APP_SECRET` (or `CRON_SECRET`) signs the session cookie.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Whole site returns `{"detail":"Not Found"}` | `vercel.json` must NOT have a catch-all rewrite to `/api/index`; the FastAPI preset already routes every path to the function while preserving the real URL |
| `CRON_SECRET/WEBHOOK_SECRET not configured` (500) | The Vercel env var exists but has no value. Re-add it with the real value (Sensitive type), then redeploy |
| Dashboard chat says it is not configured | Set `DASHBOARD_PASSWORD` (+ `APP_SECRET`) on the deployment, then reload |
| Bot silent | TELEGRAM_CHAT_ID mismatch; tap Start on bot; check `getWebhookInfo` shows the `/api/telegram` URL; check webhook secret_token |
| `/week` empty | CANVAS_TERM exact mismatch |
| Token rejected | New Canvas token (password change/expiry) → rotate in Vercel + `.env` |
| Digest twice | Check `digests` unique (local_date,channel,kind); concurrent ticks → one wins |
| Slack 429/5xx | Retried with Retry-After; check webhook URL single-channel |
| Build fails secrets | Remove real tokens from tree; bundle scanner blocks webhooks/keys/DB URLs |
