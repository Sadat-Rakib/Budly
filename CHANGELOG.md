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

### Fixed

- httpx request logging could print the Telegram bot token in URLs; those logs are
  now silenced.
- Announcement change events no longer duplicate the same announcement twice in
  "what's new" answers.
