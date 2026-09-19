# Changelog

## 0.1.0 — 2026-09-19

- Rebrand CanvasBuddy → StudyBuddy (keep package `canvasbuddy`), author Mir Sadat Bin Rakib, MIT + original copyright kept.
- Serverless notifier: Vercel `api/index.py` (Telegram webhook + cron tick), `slots.py` pure schedule, `notify/` Telegram + Slack fan-out with per-channel idempotency, `run_tick` + dry-run.
- Content: daily digest (existing) + Slack renderer, evening nudge, Saturday 08:00 review (missed open/closed, last-7d, quizzes, next-7d, changes), Saturday 15:00 check-in.
- `canvasbuddy setup` wizard + `doctor` extended (slots, Slack, secrets). `/testnotify` command.
- Landing page `site/` (no JS/cookies/trackers) + `scripts/build_release.py` (secret scan, placeholders, SHA256SUMS, LICENSE.txt).
- Docs: README, SETUP, docs/OPERATIONS. $0 infra: Vercel Hobby + Supabase free + cron-job.org.
