# StudyBuddy

Canvas has everything you need for school.

The problem is that the information is spread across courses, assignments, announcements, modules and deadlines.

Sometimes you check Canvas and still miss something.

StudyBuddy was made to fix that. Connect your Canvas account once and StudyBuddy keeps track of what is happening for you.

Ask things like:

- What's due this week?
- Did anything change today?
- What is my next assignment?
- Any new announcements?
- What's due for AUSTA?

StudyBuddy also prepares a morning and evening update so you can quickly see what needs your attention.

On the web it looks like this: a small blue robot called Postbot sits on your dashboard, watches your cursor, and answers questions about your courses. Around it live the rest of the picture: your latest digest, the sync status, and the notification channels you use.

## Why I built it

I got tired of opening Canvas, checking every course, going through announcements, assignments and modules, then still worrying that I missed something.

Missing one small update can mean missing a deadline or losing marks.

StudyBuddy gives that information one place to live.

## How it works

1. Connect StudyBuddy to Canvas with a read-only access token.
2. StudyBuddy reads your courses and upcoming work, and keeps that information updated.
3. Ask the mascot questions in normal language. Common questions (due dates, overdue work, new announcements, changes) are answered straight from your synced data, so they are fast and cannot be invented.
4. For anything else, StudyBuddy passes only the relevant course facts to an AI model and asks it to phrase the answer.
5. Get a morning and evening summary of what matters, on the dashboard and in Telegram or Slack if you use them.

Every answer links back to the source item in Canvas.

## What it can do

- Connect to Canvas LMS (read-only)
- Track courses for a term
- Find upcoming assignments, including ones with no due date set
- Surface new announcements
- Detect important changes: new and removed assignments, moved deadlines
- Answer Canvas questions in the dashboard chat
- Show source links back to Canvas
- Send morning summaries, evening reminders and a Saturday review
- Work with a replaceable AI provider (works without one for the common questions)
- Run online, without your laptop
- Self-host from GitHub

## Your data

StudyBuddy is open source and self-hostable. It runs in accounts you control.

Your Canvas credentials live only in your StudyBuddy deployment, never in the browser and never in the repository. The dashboard chat is protected by a password you set, and the Canvas token is only ever read on the server.

StudyBuddy requests the Canvas information it needs: courses, assignments, announcements. Nothing is submitted back to Canvas.

If you configure an AI provider, StudyBuddy sends that provider only the small set of course facts needed to answer your question (course codes, titles, due dates). You can leave the AI provider unconfigured: the common questions still work, answered directly from your data.

## Running it

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). On Windows, macOS or Linux:

```bash
git clone https://github.com/Sadat-Rakib/StudyBuddy
cd StudyBuddy
uv sync
cp .env.example .env
```

Then fill in `.env`. Four values matter:

| Variable | What it is | Where to get it |
| --- | --- | --- |
| `CANVAS_BASE_URL` | Your school's Canvas address | The same address you use in the browser, e.g. `https://canvas.ualberta.ca` |
| `CANVAS_TOKEN` | A Canvas personal access token | Canvas → Account → Settings → New Access Token. Read-only scope is enough |
| `DATABASE_URL` | A Postgres database | Supabase free tier works. Use the session pooler connection string |
| `USER_TIMEZONE` | Your IANA timezone | e.g. `America/Edmonton`, `America/Toronto`, `Asia/Dhaka` |

Optional but useful:

| Variable | What it is |
| --- | --- |
| `DASHBOARD_PASSWORD` | Unlocks the web dashboard chat. Leave empty and the dashboard stays locked |
| `APP_SECRET` | Random string that signs dashboard sessions. Falls back to `CRON_SECRET` |
| `OPENROUTER_API_KEY` | AI provider for the questions that need one. [openrouter.ai](https://openrouter.ai), free models available |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Delivers digests to Telegram |
| `SLACK_WEBHOOK_URL` | Delivers digests to Slack |
| `DIGEST_SLOT` / `NUDGE_SLOT` / `REVIEW_SLOT` / `CHECKIN_SLOT` | When each message goes out, e.g. `daily@07:00`. Empty string disables a slot |

Once `.env` is filled in:

```bash
uv run alembic upgrade head    # create the tables
uv run canvasbuddy sync        # first Canvas sync
uv run canvasbuddy             # list every command
```

### Running the dashboard locally

```bash
uv run uvicorn api.index:app --reload
```

Then open http://127.0.0.1:8000, sign in with `DASHBOARD_PASSWORD`, and ask Postbot something.

### Developing without a Canvas account

Set `CANVAS_MOCK_MODE=true` and StudyBuddy swaps Canvas for a built-in fixture world: three courses with work due today, tomorrow and later, an overdue item, a completed and graded item, undated graded work, and recent announcements. Sync once to bootstrap, sync again and the fixture term moves on (a deadline moves, new work and an announcement appear) so change detection, digests and "what's new?" all have something true to answer. The dashboard shows a Demo data badge, sync results carry `mock: true`, and the client logs a warning on every start, so it can never pass silently. Never enable it in production.

### Deploying it online

The hosted version runs entirely on free tiers and does not need your laptop:

1. **Vercel** hosts everything: the dashboard page, the API and the Telegram webhook. `vercel` CLI → import the repo → add the variables from `.env` in the project settings → deploy.
2. **Supabase** is the Postgres database.
3. **A cron ping** wakes the scheduler. Create a free cron-job.org job that GETs `https://your-deployment.vercel.app/api/cron` every 15 minutes with the header `Authorization: Bearer <your CRON_SECRET>`. At each tick StudyBuddy decides for itself whether a digest is due in the user's timezone, and syncs at most once an hour between digests.

No local process, database or cron is needed for the hosted version.

## Architecture

```
             ┌─────────────┐
             │   Canvas    │
             └──────┬──────┘
                    │ read-only
                    ▼
             ┌─────────────┐
             │ StudyBuddy  │  Vercel serverless (FastAPI)
             │   Backend   │  ├─ web dashboard API
             │             │  ├─ Telegram webhook
             └───┬─────┬───┘  └─ cron tick
                 │     │
          ┌──────┘     └──────┐
          ▼                   ▼
      Postgres              AI provider
      (Supabase)            (OpenRouter, optional)
          │
          ▼
    StudyBuddy dashboard + Telegram / Slack
```

The database keeps a small, normalized copy of your Canvas work: courses, assignments, announcements, exams, change events, digests and chat history. Change detection runs during every sync, so "what changed today?" is a lookup, not a guess.

## Tests

```bash
uv run pytest
```

The suite covers the Canvas sync and change detection, the digest builders, the chat engine's grounding (including a test that asks about work that does not exist and expects an honest "couldn't find it"), the dashboard auth, and the API surface.

## Bot commands

If you use the Telegram bot: `/today /week /grades /exams /digest /extract /sync /add /ics /mute /unmute /testnotify /help`

## Costs

Vercel Hobby $0, Supabase free $0, cron-job.org free $0, Slack webhook $0. OpenRouter has free models; leave it out and the dashboard still answers the common questions.

## Acknowledgements

Postbot comes from the [page-mascot](https://github.com/nilbuild/page-mascot) character set by Kamran Ahmed (MIT).

## Licence

MIT — see `LICENSE`. Canvas is a trademark of Instructure, Inc. This project is independent and not affiliated with or endorsed by Instructure.
