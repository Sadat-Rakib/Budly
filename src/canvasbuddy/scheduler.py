"""Budly's local scheduler: digests and background syncs, in-process.

Budly is local-first, so there is no external cron. A single asyncio task wakes
every minute and asks the same question the hosted cron tick asks: is a
notification slot due right now? All the real logic -- slot windows, sync
before send, per-channel idempotency, muted windows -- lives in
:mod:`canvasbuddy.notify.service` and is shared with the legacy hosted path.

Two behaviours are local-specific:

* **Catch-up.** The computer may have been asleep when a digest was scheduled.
  On startup, any slot whose time already passed today (within a bounded
  window) and that has not been sent yet runs immediately. Stale digests from
  previous days are never resurrected.
* **Restart safety.** Every send is deduplicated by the digests table's unique
  (local_date, channel, kind) row, so restarting Budly ten times a day still
  produces at most one morning Telegram message.

Budly cannot send anything while the computer is off; the README says this
plainly. Catch-up only covers the wake-up case, within the window below.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import func, select

from canvasbuddy.config import Settings, get_settings
from canvasbuddy.db import session_scope
from canvasbuddy.models import Digest

log = logging.getLogger(__name__)

#: How often the loop re-evaluates slots and the auto-sync timer.
CHECK_INTERVAL_SECONDS = 60.0
#: How long after a slot's scheduled time a missed digest may still be caught up.
CATCHUP_WINDOW = timedelta(hours=6)

_task: asyncio.Task | None = None
_last_auto_sync: datetime | None = None


def scheduler_running() -> bool:
    """True while the in-process scheduler task is alive (surfaced on /api/status)."""
    return _task is not None and not _task.done()


def start_scheduler() -> None:
    """Start the loop once per process. Calling it again is a no-op."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_running_loop().create_task(_scheduler_loop(), name="budly-scheduler")
    log.info("Budly scheduler started")


async def stop_scheduler() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    log.info("Budly scheduler stopped")


def _slot_today(slot, now: datetime) -> datetime:
    return datetime.combine(now.date(), time(slot.hour, slot.minute), tzinfo=now.tzinfo)


async def _catch_up(settings: Settings) -> None:
    """Run today's missed slots, newest last, within the catch-up window.

    A digest generated at 08:20 for a 07:00 slot is a *catch-up*, not a stale
    resend: the digests table guarantees it happens at most once today.
    """
    from canvasbuddy.notify.service import run_tick

    now = datetime.now(settings.tz)
    for slot in sorted(settings.active_slots(), key=lambda s: (s.hour, s.minute)):
        slot_time = _slot_today(slot, now)
        if not (slot_time <= now <= slot_time + CATCHUP_WINDOW):
            continue
        async with session_scope() as session:
            sent = await session.scalar(
                select(func.count())
                .select_from(Digest)
                .where(Digest.local_date == now.date(), Digest.kind == slot.kind)
            )
        if sent:
            continue
        log.info(
            "catch-up: %s slot %02d:%02d was missed; generating now",
            slot.role,
            slot.hour,
            slot.minute,
        )
        try:
            await run_tick(settings, None, now_override=slot_time.isoformat())
        except Exception:  # noqa: BLE001 - one missed slot must not stop the rest
            log.exception("catch-up for %s failed", slot.role)


async def _scheduler_loop() -> None:
    global _last_auto_sync

    settings = get_settings()
    try:
        await _catch_up(settings)
    except Exception:  # noqa: BLE001
        log.exception("startup catch-up failed")

    from canvasbuddy.notify.service import run_tick, sync_now
    from canvasbuddy.slots import due_slots

    while True:
        try:
            now = datetime.now(UTC)
            now_local = now.astimezone(settings.tz)
            due = due_slots(
                now_local,
                settings.active_slots(),
                timedelta(minutes=settings.notify_grace_minutes),
                review_replaces_daily=settings.review_replaces_daily,
            )
            if due:
                await run_tick(settings, None)
            else:
                interval = timedelta(minutes=settings.canvas_sync_interval_minutes)
                if _last_auto_sync is None or now - _last_auto_sync >= interval:
                    _last_auto_sync = now
                    if settings.canvas_configured:
                        await sync_now(settings)
        except Exception:  # noqa: BLE001 - the scheduler must outlive any single failure
            log.exception("scheduler tick failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
