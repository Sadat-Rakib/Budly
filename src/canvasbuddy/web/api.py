"""HTTP endpoints for the local Budly dashboard.

Budly binds to localhost and has no login: the person running the process is the
person whose Canvas token is in the local configuration. There is nothing to
authenticate against because there is nobody else on the other end of 127.0.0.1.
(If you deliberately expose Budly to a network, securing that deployment is your
job -- the README says so plainly.)

Routes follow what the page actually needs:

* ``GET  /api/status``  — everything the dashboard renders before the first chat
  turn: configuration state, last sync, course list, workload counts.
* ``POST /api/canvas/test`` — "Test connection" for the setup flow.
* ``POST /api/sync``    — manual "Refresh Canvas".
* ``POST /api/chat``    — one question, one grounded answer with sources.
* ``GET  /api/digests/latest`` / ``POST /api/digests/generate`` — the in-app digest.

Canvas tokens never appear in any of this. They stay in the local configuration
and are only ever read server-side; the page sees derived facts only.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from canvasbuddy.canvas.client import CanvasError, TokenRevokedError, open_canvas_client
from canvasbuddy.config import get_settings
from canvasbuddy.db import session_scope
from canvasbuddy.digest.builder import build_digest
from canvasbuddy.models import Announcement, Course, Digest
from canvasbuddy.notify.builders import render_digest_web
from canvasbuddy.notify.service import sync_now
from canvasbuddy.web import chat as web_chat

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


class ChatBody(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _bounded(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 2000:
            raise ValueError("message too long")
        return v


@router.get("/status")
async def status(request: Request) -> dict:
    settings = get_settings()
    async with session_scope() as session:
        from canvasbuddy.settings_store import get as settings_get

        last_sync_raw = await settings_get(session, "last_sync_at")
        last_error = await settings_get(session, "last_sync_error")
        courses = (
            (
                await session.scalars(
                    select(Course)
                    .where(Course.is_tracked, Course.is_active)
                    .order_by(Course.short_code)
                )
            ).all()
        )
        due_tool = web_chat.TOOLS_BY_NAME["list_upcoming"]
        upcoming = await due_tool.fn(session, settings, days=7)
        overdue_tool = web_chat.TOOLS_BY_NAME["list_overdue"]
        overdue = await overdue_tool.fn(session, settings)

        today = datetime.now(settings.tz).date()
        week = list(upcoming.get("due") or []) if isinstance(upcoming, dict) else []
        due_today = 0
        for row in week:
            due_at = web_chat._parse_local(row.get("due_at"))
            if due_at and due_at.astimezone(settings.tz).date() == today:
                due_today += 1

        week_ago = datetime.now(UTC) - timedelta(days=7)
        announcements_week = len(
            (
                await session.scalars(
                    select(Announcement.id).where(
                        Announcement.posted_at >= week_ago,
                        Announcement.course_id.in_({c.id for c in courses} or {0}),
                    )
                )
            ).all()
        )

    last_sync_at = None
    if last_sync_raw:
        try:
            last_sync_at = datetime.fromisoformat(last_sync_raw).isoformat()
        except ValueError:
            last_sync_at = None

    from canvasbuddy.scheduler import scheduler_running

    host = settings.canvas_base_url.split("/api/v1")[0] if settings.canvas_base_url else ""
    return {
        "user_name": settings.user_name,
        "timezone": settings.user_timezone,
        "server_time": datetime.now(UTC).isoformat(),
        "canvas": {
            "configured": settings.canvas_configured,
            "mock": settings.canvas_mock_mode,
            "base_url": host,
            "last_sync_at": last_sync_at,
            "last_sync_error": last_error,
        },
        "ai": {"configured": settings.openrouter_configured, "provider": "openrouter"},
        "channels": {
            "telegram": settings.telegram_configured,
            "slack": settings.slack_configured,
        },
        "scheduler": {"running": scheduler_running()},
        "slots": {
            "digest": settings.digest_slot,
            "nudge": settings.nudge_slot,
            "review": settings.review_slot,
            "checkin": settings.checkin_slot,
        },
        "courses": [
            {"code": c.short_code or c.code, "label": c.label, "term": c.term_name}
            for c in courses
        ],
        "counts": {
            "due_today": due_today,
            "due_week": len(week),
            "overdue": int(overdue.get("count") or 0) if isinstance(overdue, dict) else 0,
            "announcements_week": announcements_week,
        },
    }


@router.post("/canvas/test")
async def test_canvas() -> dict:
    """The setup flow's "Test connection": one lightweight profile read.

    Never prints the token; every failure maps to a sentence a person can act on.
    """
    settings = get_settings()
    if not settings.canvas_configured:
        return {
            "ok": False,
            "message": "Canvas is not configured yet. Add your Canvas URL and access "
            "token to .env, then restart Budly.",
        }
    try:
        async with open_canvas_client(settings) as client:
            me = await client.get_self()
        name = me.get("name") or "your Canvas account"
        return {"ok": True, "message": f"Canvas connected successfully — {name}."}
    except TokenRevokedError:
        return {
            "ok": False,
            "message": "Budly couldn't connect to Canvas: the access token was "
            "rejected. Create a new token and try again.",
        }
    except CanvasError as exc:
        log.info("canvas test failed: %s", exc)
        return {
            "ok": False,
            "message": "Budly couldn't connect to Canvas. Check the Canvas URL and "
            "access token.",
        }


@router.post("/sync")
async def sync() -> dict:
    result = await sync_now(get_settings())
    return result


@router.get("/courses")
async def courses() -> dict:
    settings = get_settings()
    async with session_scope() as session:
        rows = (
            (
                await session.scalars(
                    select(Course)
                    .where(Course.is_tracked, Course.is_active)
                    .order_by(Course.short_code)
                )
            ).all()
        )
        out = []
        for c in rows:
            upcoming = await web_chat.TOOLS_BY_NAME["list_upcoming"].fn(
                session, settings, days=31, course_code=c.short_code or c.code
            )
            due = list(upcoming.get("due") or []) if isinstance(upcoming, dict) else []
            out.append(
                {
                    "code": c.short_code or c.code,
                    "name": c.nickname or c.title or c.name,
                    "term": c.term_name,
                    "sections": c.enrolled_sections or [],
                    "upcoming_count": len(due),
                    "next_due": due[0]["due_at"] if due else None,
                }
            )
    return {"courses": out, "tracked": len(out)}


@router.post("/chat")
async def chat(body: ChatBody) -> dict:
    settings = get_settings()
    llm = await get_llm()
    try:
        async with session_scope() as session:
            answer = await web_chat.answer_message(session, settings, llm, body.message)
    except Exception as exc:  # noqa: BLE001
        log.exception("chat failed")
        raise HTTPException(
            503, "Budly hit a problem answering that. Try again in a moment."
        ) from exc
    return answer.as_dict()


@router.get("/digests/latest")
async def latest_digest() -> dict:
    settings = get_settings()
    async with session_scope() as session:
        row = await session.scalar(
            select(Digest).where(Digest.channel == "web").order_by(Digest.sent_at.desc()).limit(1)
        )
        today = datetime.now(settings.tz).date()
        if row is not None and row.local_date == today:
            return {
                "kind": row.kind,
                "local_date": row.local_date.isoformat(),
                "sent_at": row.sent_at.isoformat(),
                "body": row.body_md,
                "fresh": False,
            }

        # No digest stored for today (the scheduler hasn't fired yet, or this is a
        # fresh install): build one live so the card is current rather than stale.
        content = await build_digest(session, settings)
        return {
            "kind": "live",
            "local_date": today.isoformat(),
            "sent_at": datetime.now(UTC).isoformat(),
            "body": render_digest_web(content, settings),
            "fresh": True,
        }


@router.post("/digests/generate")
async def generate_digest() -> dict:
    """Build and store today's web digest now. Idempotent per day."""
    settings = get_settings()
    from sqlalchemy.exc import IntegrityError

    async with session_scope() as session:
        content = await build_digest(session, settings)
        body_md = render_digest_web(content, settings)
        local_date = datetime.now(settings.tz).date()
        session.add(
            Digest(
                local_date=local_date,
                channel="web",
                kind="digest",
                body_md=body_md,
                items={"announcement_ids": content.announcement_ids, "trigger": "manual"},
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            return {"ok": True, "result": "already_generated", "body": body_md}
    return {"ok": True, "result": "generated", "body": body_md}


_llm = None
_llm_lock = None  # created lazily inside the running loop


async def get_llm():
    """The shared OpenRouter client for chat, built once per process."""
    global _llm, _llm_lock
    import asyncio

    if _llm_lock is None:
        _llm_lock = asyncio.Lock()
    if _llm is None:
        from canvasbuddy.llm.openrouter import OpenRouterClient

        async with _llm_lock:
            if _llm is None:
                settings = get_settings()
                if not settings.openrouter_configured:
                    return None
                try:
                    _llm = OpenRouterClient(settings)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not build LLM client: %s", exc)
                    return None
    return _llm


__all__ = ["router", "get_llm"]
