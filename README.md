# StudyBuddy

**Your Canvas courses, in Telegram and Slack.**

StudyBuddy watches your Canvas account and messages you:

- ☀️ **Every morning 07:00** — what's due, what's new
- ⏰ **Evening 20:00** — one line, only if something's due in 24h
- 📅 **Saturday 08:00** — weekly review: missed → still open vs closed, tests ahead, workload, changes
- ☕ **Saturday 15:00** — check-in: what's still open

All times `America/Edmonton`. Built by Mir Sadat Bin Rakib. MIT licence.

It works with any school that uses Canvas (tested on `https://canvas.ualberta.ca`). Read-only — never submits or changes anything.

## Quick start

1. Download the zip from the landing page (or clone this repo).
2. `uv sync`
3. `uv run canvasbuddy setup` — guided wizard (~30 min, your own free accounts)
4. `uv run alembic upgrade head && uv run canvasbuddy sync`
5. Deploy to Vercel (bot) + Vercel (site) + cron-job.org — see `SETUP.md`.
6. Send `/testnotify review` to your bot.

## Bot commands

`/today /week /grades /exams /digest /extract /sync /add /ics /mute /unmute /testnotify /help`

## Costs

Vercel Hobby $0, Supabase free $0, cron-job.org free $0, Slack webhook $0. OpenRouter optional, off by default.

## Privacy

Runs in your own accounts. Single-user: only your Telegram chat ID is answered. No cookies/trackers on the landing page.

## Licence

MIT — see `LICENSE`.
