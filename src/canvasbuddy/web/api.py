"""HTTP endpoints for the web dashboard.

Route shape follows what the page actually needs, not a generic REST sketch:

* ``POST /api/login`` / ``POST /api/logout`` — the session cookie lifecycle.
* ``GET  /api/status`` — everything the dashboard renders before the first chat turn:
  connection health, last sync, course list, workload counts.
* ``POST /api/sync`` — manual "Refresh Canvas".
* ``POST /api/chat`` — one question, one grounded answer with sources.
* ``GET  /api/digests/latest`` — today's digest for the dashboard card.
* ``POST /api/digests/generate`` — build and store one now (idempotent per day).

Everything except login is behind the session cookie. The Canvas token never leaves
the server: these handlers talk to it through the same CanvasClient the bot and cron
use, and only the derived facts (course codes, due dates, links) ever reach the page.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from canvasbuddy.config import get_settings
from canvasbuddy.db import session_scope
from canvasbuddy.digest.builder import build_digest
from canvasbuddy.models import Announcement, Course, Digest
from canvasbuddy.notify.builders import render_digest_web
from canvasbuddy.notify.service import sync_now
from canvasbuddy.web import chat as web_chat
from canvasbuddy.web.session import (
    LoginGate,
    check_password,
    clear_cookie_header,
    cookie_header,
    mint_session,
    verify_session,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_login_gate = LoginGate()

_llm = None
_llm_lock = None  # created lazily inside the running loop; see get_llm


async def get_llm():
    """The shared OpenRouter client for chat, built once per instance."""
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


def _session_cookie(request: Request) -> str | None:
    return request.cookies.get("sb_session")


def require_session(request: Request) -> None:
    """Raise 401 unless the request carries a valid session cookie."""
    settings = get_settings()
    if not settings.dashboard_login_enabled:
        raise HTTPException(503, "dashboard is not configured (set DASHBOARD_PASSWORD)")
    if not verify_session(settings, _session_cookie(request)):
        raise HTTPException(401, "sign in required")


def _secure_cookie(request: Request) -> bool:
    # Local development runs plain http; everything else gets the Secure flag.
    host = (request.url.hostname or "").rsplit(".", 1)[0]
    return not (request.url.hostname in {"localhost", "127.0.0.1", "0.0.0.0"} or host == "127")


class LoginBody(BaseModel):
    password: str

    @field_validator("password")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("password is required")
        return v


class ChatBody(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _bounded(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 2000:
            raise ValueError("message too long")
        return v


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict:
    settings = get_settings()
    if not settings.dashboard_password:
        raise HTTPException(503, "dashboard is not configured (set DASHBOARD_PASSWORD)")
    if get_settings().session_secret is None:
        raise HTTPException(503, "no session secret configured (set APP_SECRET)")

    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    if not _login_gate.allows(client_ip, now=now):
        raise HTTPException(429, "too many attempts — wait five minutes and try again")

    if not check_password(settings, body.password):
        _login_gate.record_failure(client_ip, now=now)
        raise HTTPException(401, "wrong password")

    token = mint_session(settings)
    response.headers["Set-Cookie"] = cookie_header(token, secure=_secure_cookie(request))
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.headers["Set-Cookie"] = clear_cookie_header()
    return {"ok": True}


@router.get("/status")
async def status(request: Request) -> dict:
    require_session(request)
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

    host = settings.canvas_base_url.split("/api/v1")[0]
    return {
        "user_name": settings.user_name,
        "timezone": settings.user_timezone,
        "server_time": datetime.now(UTC).isoformat(),
        "canvas": {
            "configured": True,
            "base_url": host,
            "last_sync_at": last_sync_at,
            "last_sync_error": last_error,
        },
        "ai": {"configured": settings.openrouter_configured, "provider": "openrouter"},
        "channels": {
            "telegram": settings.telegram_configured,
            "slack": settings.slack_configured,
        },
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


@router.post("/sync")
async def sync(request: Request) -> dict:
    require_session(request)
    result = await sync_now(get_settings())
    if not result["ok"]:
        # The sync ran and failed; that is a report, not a crash.
        return result
    return result


@router.get("/courses")
async def courses(request: Request) -> dict:
    require_session(request)
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
async def chat(body: ChatBody, request: Request) -> dict:
    require_session(request)
    settings = get_settings()
    llm = await get_llm()
    try:
        async with session_scope() as session:
            answer = await web_chat.answer_message(session, settings, llm, body.message)
    except Exception as exc:  # noqa: BLE001
        log.exception("chat failed")
        raise HTTPException(
            503, "StudyBuddy's server hit a problem answering that. Try again in a moment."
        ) from exc
    return answer.as_dict()


@router.get("/digests/latest")
async def latest_digest(request: Request) -> dict:
    require_session(request)
    settings = get_settings()
    async with session_scope() as session:
        row = await session.scalar(
            select(Digest)
            .where(Digest.channel == "web")
            .order_by(Digest.sent_at.desc())
            .limit(1)
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

        # No digest stored for today (cron hasn't fired yet, or this is a fresh
        # deployment): build one live so the card is current rather than stale.
        content = await build_digest(session, settings)
        return {
            "kind": "live",
            "local_date": today.isoformat(),
            "sent_at": datetime.now(UTC).isoformat(),
            "body": render_digest_web(content, settings),
            "fresh": True,
        }


@router.post("/digests/generate")
async def generate_digest(request: Request) -> dict:
    """Build and store today's web digest now. Idempotent per day."""
    require_session(request)
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


__all__ = ["router", "require_session"]
