"""Cron tick: decide what's due, sync, fan-out to Telegram|Slack idempotently."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from canvasbuddy.canvas.client import CanvasClient, TokenRevokedError
from canvasbuddy.config import Settings
from canvasbuddy.db import session_scope
from canvasbuddy.digest.builder import build_digest, render_digest
from canvasbuddy.models import Digest
from canvasbuddy.notify.builders import (
    build_checkin_content,
    build_nudge_content,
    build_review_content,
    render_digest_slack,
)
from canvasbuddy.notify.content import NotificationContent
from canvasbuddy.notify.notifiers import (
    SlackNotifier,
    TelegramNotifier,
    render_slack_chunks,
    render_telegram_chunks,
)
from canvasbuddy.settings_store import get as settings_get
from canvasbuddy.settings_store import is_muted
from canvasbuddy.settings_store import put as settings_put
from canvasbuddy.slots import Slot

log = logging.getLogger(__name__)


def _parse_now_override(raw: str | None, tz) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(UTC)


async def _maybe_sync(settings: Settings, force: bool) -> tuple[bool, str | None]:
    """Sync if forced (slot due) or >60min since last_sync_at. Returns (synced, error)."""
    if not force:
        try:
            async with session_scope() as s:
                raw = await settings_get(s, "last_sync_at")
                if raw:
                    last = datetime.fromisoformat(raw)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=UTC)
                    if datetime.now(UTC) - last < timedelta(minutes=60):
                        return False, None
        except Exception:
            pass
    try:
        async with CanvasClient(settings) as client, session_scope() as session:
            report = await sync_all_safe(session, client, settings)
        async with session_scope() as s:
            await settings_put(s, "last_sync_at", datetime.now(UTC).isoformat())
        log.info("tick sync: %s", report.summary().replace("\n", " | ") if report else "ok")
        return True, None
    except TokenRevokedError as exc:
        return False, f"Canvas token rejected: {exc}"
    except Exception as exc:  # noqa: BLE001
        log.exception("tick sync failed")
        return False, f"{type(exc).__name__}: {exc}"


async def sync_all_safe(session, client, settings):
    from canvasbuddy.sync.worker import sync_all

    return await sync_all(session, client, settings)


async def _send_telegram(settings: Settings, payload: str) -> str:
    n = TelegramNotifier(settings)
    return await n.send(payload)


async def _send_slack(settings: Settings, payload: str) -> str:
    n = SlackNotifier(settings)
    return await n.send(payload)


async def _fanout_one_slot(
    settings: Settings,
    *,
    kind: str,
    local_date,
    telegram_payloads: list[str],
    slack_payloads: list[str],
    items: dict,
) -> dict[str, str]:
    """Per-channel own DB session with unique (local_date, channel, kind)."""
    results: dict[str, str] = {}
    enabled = []
    if settings.telegram_bot_token and settings.telegram_chat_id:
        enabled.append("telegram")
    if settings.slack_webhook_url:
        enabled.append("slack")

    for ch in enabled:
        payloads = telegram_payloads if ch == "telegram" else slack_payloads
        if not payloads:
            continue
        body = "\n\n".join(payloads)[:8000]
        try:
            async with session_scope() as s:
                s.add(
                    Digest(local_date=local_date, channel=ch, kind=kind, body_md=body, items=items)
                )
                try:
                    await s.flush()
                except IntegrityError:
                    await s.rollback()
                    results[ch] = "already_sent"
                    continue
                # Send inside the same session scope: if send raises, rollback → retry next tick.
                if ch == "telegram":
                    for p in payloads:
                        await _send_telegram(settings, p)
                else:
                    for p in payloads:
                        await _send_slack(settings, p)
                results[ch] = "sent"
        except IntegrityError:
            results[ch] = "already_sent"
        except Exception as exc:  # noqa: BLE001
            log.exception("fanout %s/%s failed", kind, ch)
            results[ch] = f"failed: {type(exc).__name__}"
    return results


async def run_tick(
    settings: Settings,
    tg_app=None,
    *,
    dry_run: bool = False,
    now_override: str | None = None,
) -> dict:
    """Main cron entrypoint. Returns JSON-serialisable dict for /api/cron."""
    from datetime import timedelta

    from canvasbuddy.slots import due_slots

    tz = settings.tz
    now_utc = _parse_now_override(now_override, tz) if now_override else None
    if now_utc is None and now_override:
        # Bad override string — still return due=[] rather than 500.
        pass
    now_utc = now_utc or datetime.now(UTC)
    now_local = now_utc.astimezone(tz)

    slots: list[Slot] = settings.active_slots()
    grace = timedelta(minutes=settings.notify_grace_minutes)
    due = due_slots(now_local, slots, grace, review_replaces_daily=settings.review_replaces_daily)
    due_kinds = [s.role for s in due]

    # --- dry run: render only ---
    if dry_run:
        rendered: dict[str, dict] = {}
        async with session_scope() as session:
            for slot in due:
                kind = slot.role
                if kind == "digest":
                    content = await build_digest(session, settings, now=now_utc)
                    tg_text = render_digest(content, settings)
                    sl_text = render_digest_slack(content, settings)
                    rendered[kind] = {"telegram": [tg_text], "slack": [sl_text]}
                elif kind == "nudge":
                    nc = await build_nudge_content(session, settings, now=now_utc)
                    if nc is None:
                        rendered[kind] = {"telegram": [], "slack": [], "note": "nothing to say"}
                    else:
                        rendered[kind] = {
                            "telegram": render_telegram_chunks(nc),
                            "slack": render_slack_chunks(nc),
                        }
                elif kind == "review":
                    nc = await build_review_content(session, settings, now=now_utc)
                    rendered[kind] = {
                        "telegram": render_telegram_chunks(nc),
                        "slack": render_slack_chunks(nc),
                    }
                elif kind == "checkin":
                    nc = await build_checkin_content(session, settings, now=now_utc)
                    rendered[kind] = {
                        "telegram": render_telegram_chunks(nc),
                        "slack": render_slack_chunks(nc),
                    }
        return {
            "ok": True,
            "dry_run": True,
            "local_time": now_local.isoformat(),
            "due": due_kinds,
            "rendered": rendered,
        }

    # --- real tick: sync ---
    synced = False
    sync_error: str | None = None
    if due:
        synced, sync_error = await _maybe_sync(settings, force=True)
        if sync_error and "rejected" in sync_error:
            # TokenRevoked alert (keep working under new tick).
            try:
                if settings.telegram_bot_token and settings.telegram_chat_id:
                    n = TelegramNotifier(settings)
                    await n.send(
                        "⚠️ StudyBuddy can't reach Canvas — the access token was rejected.\n\n"
                        f"{sync_error}"
                    )
            except Exception:
                pass
            return {
                "ok": False,
                "local_time": now_local.isoformat(),
                "synced": synced,
                "due": due_kinds,
                "error": sync_error,
                "results": {},
            }
    else:
        synced, _ = await _maybe_sync(settings, force=False)

    if not due:
        return {
            "ok": True,
            "local_time": now_local.isoformat(),
            "synced": synced,
            "due": [],
            "results": {},
        }

    # muted → skip sends (syncing already done above)
    async with session_scope() as s:
        muted_until = await is_muted(s)
    if muted_until:
        return {
            "ok": True,
            "local_time": now_local.isoformat(),
            "synced": synced,
            "due": due_kinds,
            "muted_until": muted_until.isoformat(),
            "results": {k: {"skipped": "muted"} for k in due_kinds},
        }

    results: dict[str, dict] = {}
    local_date = now_local.date()
    async with session_scope() as session:
        for slot in due:
            kind = slot.role
            if kind == "digest":
                content = await build_digest(session, settings, now=now_utc)
                tg_payloads = [render_digest(content, settings)]
                # split telegram digest on section boundaries if oversized
                from canvasbuddy.notify.notifiers import TELEGRAM_LIMIT

                if len(tg_payloads[0]) > TELEGRAM_LIMIT:
                    # naive paragraph split; MarkdownV2 already escaped per-line
                    text = tg_payloads[0]
                    tg_payloads = [
                        text[i : i + TELEGRAM_LIMIT] for i in range(0, len(text), TELEGRAM_LIMIT)
                    ]
                sl_text = render_digest_slack(content, settings)
                from canvasbuddy.notify.notifiers import SLACK_LIMIT

                sl_payloads = (
                    [sl_text[i : i + SLACK_LIMIT] for i in range(0, len(sl_text), SLACK_LIMIT)]
                    if len(sl_text) > SLACK_LIMIT
                    else [sl_text]
                )
                items = {"announcement_ids": content.announcement_ids, "trigger": kind}
            elif kind == "nudge":
                nc: NotificationContent | None = await build_nudge_content(
                    session, settings, now=now_utc
                )
                if nc is None:
                    results[kind] = {"skipped": "nothing to say"}
                    continue
                tg_payloads = render_telegram_chunks(nc)
                sl_payloads = render_slack_chunks(nc)
                items = {"trigger": kind}
            elif kind == "review":
                nc = await build_review_content(session, settings, now=now_utc)
                tg_payloads = render_telegram_chunks(nc)
                sl_payloads = render_slack_chunks(nc)
                items = {"trigger": kind}
            elif kind == "checkin":
                nc = await build_checkin_content(session, settings, now=now_utc)
                tg_payloads = render_telegram_chunks(nc)
                sl_payloads = render_slack_chunks(nc)
                items = {"trigger": kind}
            else:
                continue
            # Detach session use: fanout opens its own sessions per channel.
            # Expunge nothing — builders only read.
            res = await _fanout_one_slot(
                settings,
                kind=kind,
                local_date=local_date,
                telegram_payloads=tg_payloads,
                slack_payloads=sl_payloads,
                items=items,
            )
            results[kind] = res

    # Failure alert: every enabled channel failed → once/day plain Telegram alert.
    try:
        all_failed = results and all(
            all(v.startswith("failed") for v in ch.values() if isinstance(v, str))
            for ch in results.values()
        )
        if all_failed:
            async with session_scope() as s:
                today = now_local.date().isoformat()
                flag = await settings_get(s, "notify_fail_alert_on")
                if flag != today:
                    await settings_put(s, "notify_fail_alert_on", today)
                    if settings.telegram_bot_token and settings.telegram_chat_id:
                        n = TelegramNotifier(settings)
                        try:
                            await n.send(
                                "StudyBuddy failed to send "
                                f"{','.join(results.keys())} on all channels."
                            )
                        except Exception:
                            pass
    except Exception:
        pass

    return {
        "ok": True,
        "local_time": now_local.isoformat(),
        "synced": synced,
        "due": due_kinds,
        "results": results,
    }
