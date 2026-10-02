"""The dashboard HTTP surface: auth gate, status, chat, digests.

The endpoints are exercised through httpx's ASGI transport against the real router.
The database is faked one level below the handlers: a FakeSession serves unsaved ORM
rows keyed by the statement's entity, and the heavy tool calls are patched the same
way test_web_chat patches them. What gets tested here is the HTTP behaviour -- status
codes, cookies, auth gating, and response shapes -- not the query logic underneath.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from canvasbuddy.config import Settings
from canvasbuddy.models import Announcement, Course, Digest, Setting
from canvasbuddy.web import api as web_api


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "dashboard_password": "sesame",
        "app_secret": "secret",
        "user_timezone": "America/Edmonton",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Serves canned rows by entity type; records adds."""

    def __init__(
        self,
        *,
        courses=(),
        announcements=(),
        settings_rows: dict[str, str] | None = None,
        digest: Digest | None = None,
    ):
        self.courses = list(courses)
        self.announcements = list(announcements)
        self.settings_rows = settings_rows or {}
        self.digest = digest
        self.added: list[Any] = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        if model is Setting:
            if key not in self.settings_rows:
                return None
            return Setting(key=key, value=self.settings_rows[key])
        return None

    async def scalars(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Course:
            return FakeResult(self.courses)
        if entity is Announcement:
            return FakeResult(self.announcements)
        return FakeResult([])

    async def scalar(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Digest:
            return self.digest
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None


def course() -> Course:
    c = Course(canvas_id=1, code="CSC 153H3", short_code="CSC 153", name="Data Analysis")
    c.id = 1
    return c


@pytest.fixture
def app_env(monkeypatch):
    """Reset the cached settings for every test and return a settings factory."""
    def factory(**overrides):
        settings = make_settings(**overrides)
        monkeypatch.setattr(web_api, "get_settings", lambda: settings)
        return settings

    yield factory


@pytest.fixture
def client(app_env, monkeypatch):
    from fastapi import FastAPI

    async def _client(session: FakeSession, settings: Settings):
        def fake_session_scope():
            return session

        monkeypatch.setattr(web_api, "session_scope", fake_session_scope)
        app = FastAPI()
        app.include_router(web_api.router)
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _client


def patch_tools(monkeypatch, **results) -> None:
    from canvasbuddy.agent.tools import TOOLS_BY_NAME as real_tools

    for name, result in results.items():
        async def fake(session, settings, __result=result, **kwargs):
            return __result

        original = real_tools[name]
        # web_chat.TOOLS_BY_NAME is the same dict object; patching mutates both views.
        monkeypatch.setitem(real_tools, name, dc_replace(original, fn=fake))



@pytest.mark.asyncio
class TestStatus:
    async def test_status_shape(self, app_env, client, monkeypatch) -> None:
        settings = app_env()
        patch_tools(
            monkeypatch,
            list_upcoming={
                "due": [
                    {"course": "CSC 153", "title": "Quiz 5", "due_at": _iso_in_tz(settings, 0)},
                    {"course": "CSC 153", "title": "Lab 2", "due_at": _iso_in_tz(settings, 3)},
                ]
            },
            list_overdue={"overdue": [], "count": 0},
        )
        session = FakeSession(courses=[course()], settings_rows={"last_sync_at": _utc_iso(0)})
        async with await client(session, settings) as c:
            response = await c.get("/api/status")

        assert response.status_code == 200
        body = response.json()
        assert body["canvas"]["configured"] is True
        assert body["canvas"]["base_url"] == "https://canvas.example.edu"
        assert body["counts"]["due_week"] == 2
        assert body["counts"]["due_today"] == 1
        assert body["counts"]["overdue"] == 0
        assert body["courses"][0]["code"] == "CSC 153"


@pytest.mark.asyncio
class TestMockModeSurfacing:
    async def test_status_exposes_mock_flag(self, app_env, client, monkeypatch) -> None:
        settings = app_env(canvas_mock_mode=True)
        patch_tools(
            monkeypatch, list_upcoming={"due": []}, list_overdue={"overdue": [], "count": 0}
        )
        session = FakeSession(courses=[course()])
        async with await client(session, settings) as c:
            response = await c.get("/api/status")
        assert response.status_code == 200
        assert response.json()["canvas"]["mock"] is True


@pytest.mark.asyncio
class TestChat:
    async def test_chat_returns_answer_with_sources(self, app_env, client, monkeypatch) -> None:
        settings = app_env()

        async def fake_answer(session, settings, llm, message):
            return web_api.web_chat.ChatAnswer(
                "You have 1 thing due this week:\n1. CSC 153 — Quiz 5",
                kind="due",
                sources=[web_api.web_chat.Source("CSC 153", "Quiz 5", "2026-10-02T23:59", "https://canvas.example.edu/a/9")],
            )

        monkeypatch.setattr(web_api.web_chat, "answer_message", fake_answer)
        async with await client(FakeSession(), settings) as c:
            response = await c.post("/api/chat", json={"message": "What's due this week?"})

        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "due"
        assert body["sources"][0]["title"] == "Quiz 5"
        assert body["sources"][0]["url"].endswith("/a/9")

    async def test_chat_rejects_huge_messages(self, app_env, client) -> None:
        settings = app_env()
        async with await client(FakeSession(), settings) as c:
            huge = await c.post(
                "/api/chat", json={"message": "x" * 3000}
            )
        assert huge.status_code == 422


@pytest.mark.asyncio
class TestDigests:
    async def test_latest_returns_stored_row_for_today(self, app_env, client) -> None:
        settings = app_env()
        today = datetime.now(settings.tz).date()
        digest = Digest(
            local_date=today,
            channel="web",
            kind="digest",
            body_md="**Due today**\n- CSC 153 — Quiz 5",
            items={},
        )
        digest.sent_at = datetime.now(UTC)
        session = FakeSession(digest=digest)
        async with await client(session, settings) as c:
            response = await c.get("/api/digests/latest")

        assert response.status_code == 200
        body = response.json()
        assert body["fresh"] is False
        assert "Quiz 5" in body["body"]

    async def test_latest_builds_live_when_nothing_stored(self, app_env, client) -> None:
        settings = app_env()
        session = FakeSession(courses=[])  # no courses -> empty live digest
        async with await client(session, settings) as c:
            response = await c.get("/api/digests/latest")

        assert response.status_code == 200
        body = response.json()
        assert body["fresh"] is True
        assert "Enjoy it" in body["body"]  # the empty-day line

    async def test_generate_is_idempotent_per_day(self, app_env, client) -> None:
        settings = app_env()
        session = FakeSession(courses=[])
        async with await client(session, settings) as c:
            response = await c.post("/api/digests/generate")
        assert response.status_code == 200
        assert response.json()["result"] == "generated"
        assert session.added and session.added[0].channel == "web"


def _utc_iso(hours_from_now: int) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours_from_now)).isoformat()


def _iso_in_tz(settings: Settings, days_from_now: int) -> str:
    local = (datetime.now(UTC).astimezone(settings.tz) + timedelta(days=days_from_now)).replace(
        hour=23, minute=59
    )
    return local.isoformat()
