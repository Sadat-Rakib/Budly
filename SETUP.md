# Hosting notes (legacy, optional)

Budly v1.0 is local-first: see the README for the real setup (`uv run budly start`).
Nothing in this file is required to run Budly.

This repository also carries an optional, legacy hosted adapter: a Vercel function
that receives the Telegram webhook and runs the notification tick remotely for
people who want updates without keeping a process running. It is not the product;
it stores data in whatever `DATABASE_URL` points at (historically Supabase) and
exists because it was already built.

## Deploying the legacy adapter (not required)

1. `vercel link`, then add the variables from `.env` (Canvas, Telegram, Slack, the
   `WEBHOOK_SECRET` / `CRON_SECRET` pair, and a Postgres `DATABASE_URL`) in the
   Vercel project settings.
2. `vercel --prod`
3. Point the Telegram webhook at `<deployment>/api/telegram` with the webhook
   secret, and any external cron at `<deployment>/api/cron` with
   `Authorization: Bearer <CRON_SECRET>`.

The dashboard API intentionally does not run on the hosted function. The showcase
page there is a project page only; it holds no credentials and runs no dashboard.

## Troubleshooting (hosted adapter)

| Symptom | Fix |
|---|---|
| Whole site returns `{"detail":"Not Found"}` | `vercel.json` must NOT have a catch-all rewrite to `/api/index` |
| `CRON_SECRET/WEBHOOK_SECRET not configured` (500) | The env var exists but has no value; re-add it with the real value |
| Bot silent | Tap Start on the bot; check `getWebhookInfo` shows the `/api/telegram` URL |
| Token rejected | Create a new Canvas token; rotate it in Vercel and `.env` |
| Digest twice | `digests` is unique per (local_date, channel, kind); concurrent ticks cannot double-send |
| Build fails on secrets | The bundle scanner blocks webhooks/keys/DB URLs; remove real tokens from the tree |
