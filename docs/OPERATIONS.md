# Operations

## Change times

Vercel → bot project → Settings → Environment Variables:

- `DIGEST_SLOT=daily@07:00`, `NUDGE_SLOT=daily@20:00`, `REVIEW_SLOT=sat@08:00`, `CHECKIN_SLOT=sat@15:00`
- Empty string disables that role. `NOTIFY_GRACE_MINUTES=180` (late-ping window).
- `REVIEW_REPLACES_DAILY=true` (Saturday review supersedes digest/nudge).

Redeploy after saving. Validate locally first: bad specs fail fast at startup.

## Rotate tokens

- Canvas: new token → update `CANVAS_TOKEN` in Vercel + local `.env` → `uv run canvasbuddy sync`.
- Telegram: new token from @BotFather → update `TELEGRAM_BOT_TOKEN` → reset webhook with new token.
- Slack: new webhook → update `SLACK_WEBHOOK_URL`.
- Secrets: regenerate `WEBHOOK_SECRET`/`CRON_SECRET` → update Vercel + cron-job.org header + webhook `secret_token`.

## Canvas token expiry alert

`run_tick` catches `TokenRevokedError` on sync and sends a plain Telegram alert. If digests stop with no alert, check Vercel logs + `last_sync_at` in `settings`.

## Pause

`/mute 3d` / `/mute 12h` / `/unmute` in Telegram. Mute suppresses all scheduled sends on all channels; sync continues; slash commands still work.

## Update

Re-download zip, keep `.env`. `uv sync`, `alembic upgrade head`, `vercel --prod` (bot), site redeploys automatically.

## Troubleshooting

- Dry-run: `GET /api/cron?dry_run=1&now=<ISO local>` with `Authorization: Bearer` — returns due + rendered payloads, writes nothing.
- Verify `digests` table: one row per `(local_date, channel, kind)`.
- Cold-start >30s: seed DB first; non-due ticks do one `settings` read (<1s).
- `/extract` slow → Telegram may resend; document as known.
- Supabase: must be Session pooler 5432, `?ssl=require`; direct IPv6 unreachable from Vercel; transaction pooler breaks asyncpg.
