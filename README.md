# Budly

Budly keeps an eye on Canvas for you.

Canvas information is spread across courses, assignments, announcements, modules and deadlines. It is easy to miss one small update even when you check Canvas regularly.

Budly runs on your own computer and connects to your Canvas account.

You can ask things like:

- What's due this week?
- What changed today?
- Do I have anything overdue?
- Any new announcements?
- What's my next assignment?

Budly can also prepare morning and evening updates and send them to Telegram or Slack.

A small blue robot called Postbot lives on the dashboard. It follows your cursor and answers questions about your courses.

## Quick start

### 1. Download Budly

Download Budly v1.0 from the releases page, or clone the repository:

```bash
git clone https://github.com/Sadat-Rakib/StudyBuddy
cd StudyBuddy
```

### 2. Install Python and uv

Budly needs Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). On Windows:

```powershell
winget install Python.Python.3.12
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Or run the bundled setup script, which checks these for you:

```bash
setup.bat      # Windows
./setup.sh     # macOS / Linux
```

### 3. Install Budly

```bash
uv sync
```

### 4. Create your configuration

Copy `.env.example` to `.env` and fill in the values you want. Only the Canvas section is required.

### 5. Connect Canvas

In `.env`:

```bash
CANVAS_BASE_URL=https://canvas.ualberta.ca
CANVAS_TOKEN=your-token-here
```

Your Canvas base URL is the same address you use in the browser. Your personal access token comes from Canvas: Account, Settings, Approved Integrations, New Access Token. It is not your Canvas password, and it should stay private, just like a password. Some institutions restrict token creation; if you cannot create one, ask your institution's IT help desk.

Check the connection:

```bash
uv run budly test-canvas
```

Success prints "Canvas connected successfully". On failure Budly prints one plain sentence about what to check, and never prints the token.

### 6. Add an AI provider (optional)

Budly answers due-date and change questions from your synced data with no AI at all. For free-form questions, configure OpenRouter (one account, many models, free models available):

```bash
OPENROUTER_API_KEY=your-key
CHAT_MODEL=nvidia/nemotron-3-super-120b-a12b:free
AI_FALLBACK_MODEL_1=nvidia/nemotron-3-ultra-550b-a55b:free
AI_FALLBACK_MODEL_2=google/gemma-4-31b-it:free
GROQ_API_KEY=your-groq-key
GROQ_MODEL=openai/gpt-oss-120b
```

How the chain works: Budly walks its providers in order. First the OpenRouter models (the `:free` slugs cost nothing; check openrouter.ai/models for what is currently free and pick ones that list tool support), then Groq's free tier, and finally the deterministic answers from your own data. If a provider rate-limits, times out, has an outage, or a model disappears, the next one takes over automatically, so a busy day on one free tier never leaves you without an answer. A rejected API key stops immediately instead of wasting the fallbacks, and a working primary is never skipped.

Free model slugs rotate: `nvidia/nemotron-3-super-120b-a12b:free` is what the shipped defaults were validated against, but treat the exact names as maintenance items, not promises.

### 7. Optional: Telegram or Slack

Notifications are optional. Budly is useful with none, one, or both.

**Telegram:** create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`, copy the token), send any message to your new bot, then put both values in `.env`:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=your-chat-id
```

Not sure of your chat ID? `uv run budly doctor` prints it after you have messaged the bot once.

**Slack:** create a Slack app, enable Incoming Webhooks, pick a channel, copy the webhook URL:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

Test either channel:

```bash
uv run budly test-telegram   # sends: Budly is connected (a graduation cap)
uv run budly test-slack
```

### 8. Start Budly

```bash
uv run budly start
```

Then open http://127.0.0.1:8000. The dashboard loads immediately; the first Canvas sync runs in the background and fills it in.

The scheduler runs inside Budly. Morning updates go out at 07:00 (your timezone, configurable), evening updates at 20:00, plus a Saturday review. Budly checks Canvas for changes every 30 minutes (`CANVAS_SYNC_INTERVAL_MINUTES=30` in `.env` to change it).

## Running locally means this

Budly runs on your computer. Scheduled notifications are sent while Budly is running and your computer is awake.

If Budly starts after a scheduled update was missed, it generates a catch-up update, within a bounded window. A morning digest from 07:00 is caught up if Budly starts by early afternoon; yesterday's news is never resurrected at midnight.

Restarting Budly never duplicates a notification: every digest is recorded once per day per channel, and a restart re-checks that record before sending.

## Mock mode (for development)

Set `CANVAS_MOCK_MODE=true` and Budly swaps Canvas for three built-in demo courses (CSC 153, COMP 214, MATH 120) with work due today, tomorrow and later, an overdue item, a completed item, and announcements. Sync once to bootstrap, sync again and the demo term moves on so change detection has something to detect. The dashboard shows a clear Demo data badge, and Budly logs a warning on every start. Never enable it in any deployment that also serves real use.

## Privacy

Budly is open source and runs on your computer.

Your Canvas access token and integration credentials are stored in your local configuration and are not committed to the repository.

Budly connects directly to Canvas to retrieve your course information. Budly binds to localhost by default; nothing it serves is reachable from other machines unless you deliberately change that, and then securing it is your responsibility.

If you configure an external AI provider, the minimum relevant Canvas context needed to answer a question may be sent to that provider. Canvas credentials are never sent to the AI provider, and simple questions are answered locally without any AI call at all.

If you enable Telegram or Slack, notification content is sent to the service you configured.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Canvas connection rejected | Re-check `CANVAS_BASE_URL` (no `/api/v1` needed) and create a fresh token. Password changes on Canvas revoke tokens. |
| "AI" answers unavailable | Set `OPENROUTER_API_KEY`. Due-date and change questions work without it. |
| Telegram message not arriving | Message the bot once (tap Start), then check `TELEGRAM_CHAT_ID`. `uv run budly doctor` shows the detected ID. |
| Slack webhook rejected | Recreate the incoming webhook; the URL must start with `https://hooks.slack.com/services/`. |
| Port already in use | Start on another port: `uv run budly start --port 8080`. |
| Scheduler not sending | Budly must be running at the scheduled time (or started within the catch-up window). Check the scheduler dot on the dashboard status line. |
| Computer was asleep during the scheduled time | Budly generates a catch-up update on start if the slot is still within its window. |
| Demo assignments appeared | `CANVAS_MOCK_MODE=true` is set in your `.env`. Remove it and restart. |
| Sync fails with `column "submission_types" is of type text[]` | Your database predates Budly v1.0 and still uses the old column types. Run `uv run alembic upgrade head` once. Local SQLite stores create themselves and need no migration. |
| Starting with `DATABASE_URL` unset still uses Postgres | An explicit value in `.env` wins. Delete the `DATABASE_URL` line to fall back to the local store in `~/.budly/budly.db`. |

## Project structure

```
src/canvasbuddy/          the application
  canvas/                 Canvas API client, payload schemas, fixture mock
  sync/                   sync worker + change detection
  digest/                 digest assembly and rendering
  notify/                 delivery to Telegram, Slack and the in-app card
  agent/                  the chat's tools, memory and model loop
  llm/                    the AI provider client (OpenRouter-compatible)
  web/                    dashboard API + chat engine
  scheduler.py            the local in-process scheduler
  server.py               the local server (what `budly start` runs)
api/index.py              legacy hosted webhook (optional, not needed locally)
public/                   the Bento dashboard page
migrations/               Postgres migrations (SQLite creates its store itself)
tests/                    the test suite
```
## Contributing

1. Fork and clone the repository.
2. `uv sync`
3. Set `CANVAS_MOCK_MODE=true` in `.env` so you never need real Canvas credentials.
4. `uv run pytest` should pass before and after your change.
5. `uv run ruff check .` must be clean.
6. Open a pull request.

## Costs

Everything local is free. An OpenRouter account has free models; leave AI unconfigured and the deterministic answers still work. Telegram and Slack webhooks are free.

## Licence

MIT - see `LICENSE`. Canvas is a trademark of Instructure, Inc. Budly is independent and not affiliated with or endorsed by Instructure. Postbot comes from the page-mascot character set (MIT, Kamran Ahmed).
