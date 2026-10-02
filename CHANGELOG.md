# Changelog

## Budly v1.0 (2026-10-02)

Budly is the new name for the StudyBuddy project, and v1.0 is the local-first release.

### The headline

Budly now runs entirely on your computer: `uv run budly start` serves the Bento
dashboard on localhost, stores everything in a local SQLite file, and runs the
notification scheduler in-process. No Supabase account, no cron service, no login.

### Added

- Local application: `budly start` binds to 127.0.0.1 and opens the dashboard with
  Postbot; no password, no signup.
- Local-first storage: SQLite by default (`~/.budly/budly.db`); Postgres still
  works as an optional adapter for existing deployments.
- Local scheduler: morning digest (07:00), evening update (20:00), Saturday review
  and check-in, run in-process with catch-up for missed slots after sleep, and
  per-day per-channel duplicate prevention.
- Background Canvas sync every 30 minutes (`CANVAS_SYNC_INTERVAL_MINUTES`), with an
  overlap guard so manual and automatic syncs never run twice at once.
- Multi-model AI fallback: primary model, then `AI_FALLBACK_MODEL_1` and
  `AI_FALLBACK_MODEL_2` on rate limits, outages, timeouts and dead models; a
  rejected key fails fast and deterministic answers never need AI at all.
- `budly test-canvas`, `budly test-telegram` and `budly test-slack` connection
  tests with human-readable outcomes.
- `CANVAS_MOCK_MODE` fixture Canvas for development without an account: three demo
  courses (CSC 153, COMP 214, MATH 120), a scripted second-sync change story, and a
  loud Demo data badge.
- `setup.bat` / `setup.sh` helpers and `setup.sh` for macOS/Linux.

### Changed

- The hosted Vercel deployment is a project page plus the optional legacy Telegram
  webhook only; the dashboard API no longer runs there.
- Removed the dashboard password and session login from the local app (localhost
  needs none).
- Real Canvas data is never rewritten by demo data; demo courses use CSC-prefixed
  names.

### Added (v1.0.1)

- Cross-provider AI chain: Groq joins OpenRouter as a second free-tier provider.
  OpenRouter reports unknown models as 400s (observed live); the chain now
  classifies those as model-unavailable and slides to the next provider, so a
  saturated free tier no longer costs an answer.

### Fixed

- A Postgres store created by an earlier version could not be written to: the v1
  models store JSON in every dialect, but the existing tables declared `text[]` and
  `jsonb`, so every sync failed with a datatype mismatch. The cross-dialect
  migration now converts those columns correctly (`to_json` for arrays, `::json` for
  jsonb) and its downgrade round-trips; a test fails if a JSON column ever appears in
  the models without a matching migration.
- `budly sync`, `budly digest` and the other commands against a brand-new SQLite
  file used to fail with "no such table": the local store now creates its schema on
  first use, not only when the dashboard starts.
- httpx request logging could print the Telegram bot token in URLs; those logs are
  now silenced.
- Announcement change events no longer duplicate the same announcement twice in
  "what's new" answers.
- `budly doctor` and `budly extract` constructed a live Canvas client even with
  `CANVAS_MOCK_MODE=true`, so the demo mode's first command reported a false
  401 from a Canvas it never talked to. Both now go through the same client
  factory as the sync path.
- The release zip contained the previous release's zip inside `public/downloads`
  (over half the bundle's size) and wrote `public/index.html` twice, with a
  stale SHA tooltip winning depending on the extractor. The bundle now ships
  each file once and no nested zips; it is 1.7 MB lighter.
