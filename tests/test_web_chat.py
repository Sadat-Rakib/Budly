"""The web chat engine: intent routing, grounding, and the hallucination guard.

The tool functions are replaced with recorders returning fixture rows, exactly as
test_agent_loop does, so the whole engine is exercised without a database. The
assertions that matter most are about what the answers *cannot* say: no invented
assignments, no invented dates, no made-up urgency.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import respx

from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import OpenRouterClient
from canvasbuddy.models import Course
from canvasbuddy.web import chat as web_chat
from canvasbuddy.web.chat import (
    _due_window,
    _since_moment,
    answer_message,
    match_course_mentions,
)

OR = "https://openrouter.ai/api/v1"


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "user_timezone": "America/Edmonton",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def course(**overrides: object) -> Course:
    c = Course(canvas_id=1, code="CSC 153H3", short_code="CSC 153", name="Data Analysis")
    c.id = 1
    for key, value in overrides.items():
        setattr(c, key, value)
    return c


def due_row(
    course_code: str = "CSC 153",
    title: str = "Quiz 5",
    due_at: str | None = "2026-10-02T23:59:00-06:00",
    url: str | None = "https://canvas.example.edu/courses/1/assignments/9",
    **extra: object,
) -> dict[str, Any]:
    row = {
        "course": course_code,
        "title": title,
        "due_at": due_at,
        "points_possible": 10.0,
        "submitted": False,
        "score": None,
        "url": url,
    }
    row.update(extra)
    return row


class FakeSession:
    """Answer_message never touches the database once the tools are patched out."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def patch_tools(monkeypatch: pytest.MonkeyPatch, **tools) -> dict[str, list[dict]]:
    """Replace tool registry entries; returns the calls each one received."""
    calls: dict[str, list[dict]] = {name: [] for name in tools}

    for name, result in tools.items():
        calls[name] = []

        async def fake(session, settings, __result=result, __name=name, **kwargs):
            calls[__name].append(kwargs)
            return __result() if callable(__result) else __result

        original = web_chat.TOOLS_BY_NAME[name]
        monkeypatch.setitem(
            web_chat.TOOLS_BY_NAME, name, replace(original, fn=fake)
        )
    return calls


def patch_courses(monkeypatch: pytest.MonkeyPatch, *courses: Course) -> None:
    async def fake_tracked(session):
        return list(courses)

    monkeypatch.setattr(web_chat, "_tracked_courses", fake_tracked)


# --------------------------------------------------------------- pure matchers


class TestMatchCourseMentions:
    def test_short_code_prefix(self) -> None:
        got = match_course_mentions("what's due for CSC?", [course()])
        assert len(got) == 1
        assert got[0][1] == "csc"

    def test_full_code_substring(self) -> None:
        got = match_course_mentions("CSC 153 quiz?", [course()])
        assert got[0][1] == "csc 153"

    def test_nickname(self) -> None:
        c = course(nickname="my data class")
        got = match_course_mentions("anything new in my data class?", [c])
        assert len(got) == 1

    def test_no_mention(self) -> None:
        assert match_course_mentions("what's due this week?", [course()]) == []

    def test_two_courses_matching_prefix_is_ambiguous(self) -> None:
        a = course()
        b = Course(canvas_id=2, code="CSC 268H3", short_code="CSC 268", name="Stats")
        b.id = 2
        got = match_course_mentions("what's due for CSC?", [a, b])
        assert {c.short_code for c, _ in got} == {"CSC 153", "CSC 268"}

    def test_specific_code_wins_over_shared_prefix(self) -> None:
        a = course()
        b = Course(canvas_id=2, code="CSC 268H3", short_code="CSC 268", name="Stats")
        b.id = 2
        got = match_course_mentions("what's due for CSC 268?", [a, b])
        assert len(got) == 1
        assert got[0][0].short_code == "CSC 268"

    def test_the_does_not_match_theatre(self) -> None:
        thea = Course(canvas_id=3, code="THEA 101H3", short_code="THEA 101", name="Theatre")
        thea.id = 3
        assert match_course_mentions("when is the show due?", [thea]) == []


class TestWindows:
    def settings(self) -> Settings:
        return make_settings()

    def test_today(self) -> None:
        now = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)  # Thursday
        start, end, label = _due_window("what's due today?", self.settings(), now)
        assert (start, end, label) == (now.astimezone(self.settings().tz).date(),) * 2 + ("today",)

    def test_tomorrow_window_is_tomorrow_only(self) -> None:
        now = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
        start, end, label = _due_window("anything due tomorrow?", self.settings(), now)
        tz = self.settings().tz
        assert start == end == now.astimezone(tz).date() + timedelta(days=1)
        assert label == "tomorrow"

    def test_bare_weekday_means_the_coming_one(self) -> None:
        now = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)  # Thursday
        _, end, label = _due_window("what's due by friday?", self.settings(), now)
        assert label == "friday"
        assert (end - now.astimezone(self.settings().tz).date()).days == 1

    def test_since_yesterday_starts_at_midnight(self) -> None:
        now = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
        since, label = _since_moment("what's new since yesterday?", self.settings(), now)
        assert label == "since yesterday"
        assert since.astimezone(self.settings().tz).hour == 0


# ------------------------------------------------------------ deterministic answers


class TestDeterministicAnswers:
    async def test_due_this_week_lists_rows_with_sources(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        calls = patch_tools(
            monkeypatch,
            list_upcoming={"due": [due_row()], "no_due_date_set": []},
        )

        answer = await answer_message(FakeSession(), make_settings(), None, "What's due this week?")

        assert answer.kind == "due"
        assert "CSC 153" in answer.text
        assert "Quiz 5" in answer.text
        assert answer.sources[0].url == "https://canvas.example.edu/courses/1/assignments/9"
        assert calls["list_upcoming"][0]["days"] == 31

    async def test_due_tomorrow_excludes_other_days(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(
            monkeypatch,
            list_upcoming={
                "due": [
                    due_row(title="Due in 3 days", due_at="2026-10-04T22:00:00-06:00"),
                ],
                "no_due_date_set": [],
            },
        )
        # 2026-10-01 local; tomorrow is 2026-10-02, so an Oct 4 row must be filtered.
        settings = make_settings()
        now = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
        rows = web_chat._rows_between(
            [
                due_row(title="Tomorrow thing", due_at="2026-10-02T22:00:00-06:00"),
                due_row(title="Later thing", due_at="2026-10-04T22:00:00-06:00"),
            ],
            settings,
            *(web_chat._due_window("due tomorrow?", settings, now)[:2]),
        )
        assert [r["title"] for r in rows] == ["Tomorrow thing"]

    async def test_overdue(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(
            monkeypatch,
            list_overdue={
                "overdue": [due_row(title="Old Lab", due_at="2026-09-20T22:00:00-06:00")],
                "count": 1,
            },
        )

        answer = await answer_message(FakeSession(), make_settings(), None, "what's overdue?")

        assert answer.kind == "overdue"
        assert "Old Lab" in answer.text

    async def test_nothing_due_is_honest(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(monkeypatch, list_upcoming={"due": [], "no_due_date_set": []})

        answer = await answer_message(FakeSession(), make_settings(), None, "What's due this week?")

        assert answer.kind == "clear"
        assert "You're clear for now" in answer.text

    async def test_changes_since(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(
            monkeypatch,
            get_changes_since={
                "changes": [
                    {
                        "type": "new_announcement",
                        "what": "Module 5 posted",
                        "course": "CSC 153",
                        "when": "2026-10-01T09:00:00-06:00",
                        "url": "https://canvas.example.edu/courses/1/discussions/5",
                    },
                    {
                        "type": "due_date_changed",
                        "what": "Lab 6",
                        "course": "COMP 214",
                        "when": "2026-10-01T10:00:00-06:00",
                        "changed": {
                            "from": "2026-10-03T22:00:00-06:00",
                            "to": "2026-10-04T22:00:00-06:00",
                        },
                    },
                ]
            },
        )

        answer = await answer_message(FakeSession(), make_settings(), None, "What changed today?")

        assert answer.kind == "changes"
        assert "Module 5 posted" in answer.text
        assert "Lab 6" in answer.text
        assert answer.sources[0].url.endswith("/discussions/5")

    async def test_no_changes_says_so(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(monkeypatch, get_changes_since={"changes": []})

        answer = await answer_message(
            FakeSession(), make_settings(), None, "Any new announcements?"
        )

        assert answer.kind == "clear"
        assert "No updates" in answer.text

    async def test_next_assignment(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(
            monkeypatch,
            list_upcoming={
                "due": [due_row(title="Quiz 5"), due_row(title="Final Project")],
                "no_due_date_set": [],
            },
        )

        answer = await answer_message(
            FakeSession(), make_settings(), None, "What's my next assignment?"
        )

        assert answer.kind == "next"
        assert answer.text.startswith("Next up: CSC 153 — Quiz 5")

    async def test_course_scoped_due_question(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(
            monkeypatch,
            list_upcoming={"due": [due_row()], "no_due_date_set": []},
        )

        answer = await answer_message(FakeSession(), make_settings(), None, "What's due for CSC?")

        assert answer.kind == "due"
        assert "CSC 153" in answer.text
        assert "Quiz 5" in answer.text

    async def test_show_everything_for_a_course(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(
            monkeypatch,
            list_upcoming={"due": [due_row()], "no_due_date_set": []},
            search_announcements={
                "matches": [
                    {"title": "Week 5 notes", "posted_at": "2026-10-01T09:00:00-06:00", "url": None}
                ]
            },
        )

        answer = await answer_message(
            FakeSession(), make_settings(), None, "Show me everything for CSC"
        )

        assert answer.kind == "course"
        assert "Week 5 notes" in answer.text


class TestHallucinationGuard:
    """PRD 57: a question about work that does not exist must not invent an answer."""

    async def test_named_item_not_in_data(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(monkeypatch, search_assignments={"query": "robotics project", "matches": []})

        answer = await answer_message(
            FakeSession(), make_settings(), None, "When is the final robotics project due?"
        )

        assert answer.kind == "not_found"
        assert "couldn't find" in answer.text
        assert "robotics project" in answer.text

    async def test_named_item_found_gets_real_dates(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        patch_tools(
            monkeypatch,
            search_assignments={
                "query": "robotics",
                "matches": [
                    due_row(title="Robotics Milestone", due_at="2026-10-09T17:00:00-06:00")
                ],
            },
        )

        answer = await answer_message(
            FakeSession(), make_settings(), None, "When is the robotics milestone due?"
        )

        assert "Robotics Milestone" in answer.text
        assert answer.sources[0].title == "Robotics Milestone"


class TestRouting:
    async def test_greeting(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        answer = await answer_message(FakeSession(), make_settings(), None, "hey!")
        assert answer.kind == "greeting"

    async def test_help(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        answer = await answer_message(FakeSession(), make_settings(), None, "what can you do?")
        assert answer.kind == "help"

    async def test_ambiguous_course_asks_back(self, monkeypatch) -> None:
        comp1 = Course(canvas_id=2, code="COMP 214H3", short_code="COMP 214", name="Databases")
        comp1.id = 2
        comp2 = Course(canvas_id=3, code="COMP 268H3", short_code="COMP 268", name="Comp Sci")
        comp2.id = 3
        patch_courses(monkeypatch, comp1, comp2)
        answer = await answer_message(FakeSession(), make_settings(), None, "What's due for COMP?")
        assert answer.kind == "clarify"
        assert "COMP 214" in answer.text and "COMP 268" in answer.text

    async def test_without_llm_unrecognised_questions_say_so(self, monkeypatch) -> None:
        patch_courses(monkeypatch, course())
        answer = await answer_message(
            FakeSession(), make_settings(), None, "write me a python game"
        )
        assert answer.kind == "ai_unavailable"


class TestAgentFallback:
    @respx.mock
    async def test_unrecognised_question_goes_to_the_tool_loop(self, monkeypatch) -> None:
        from tests.test_agent_loop import assistant_text

        patch_courses(monkeypatch, course())
        saved: list[tuple[str, str]] = []

        async def fake_save_turn(session, channel, role, content):
            saved.append((role, content))

        monkeypatch.setattr(web_chat.memory, "save_turn", fake_save_turn)

        async def fake_history(session, settings, channel):
            return []

        monkeypatch.setattr(web_chat.memory, "load_history", fake_history)

        async def fake_system_prompt(session, settings, channel="web"):
            return "system"

        monkeypatch.setattr(web_chat, "build_system_prompt", fake_system_prompt)

        respx.post(f"{OR}/chat/completions").mock(
            return_value=httpx_response(assistant_text("Here's what I know."))
        )

        settings = make_settings(openrouter_api_key="sk-or-test")
        async with OpenRouterClient(settings) as llm:
            answer = await answer_message(
                FakeSession(), settings, llm, "should I switch majors?"
            )

        assert answer.kind == "agent"
        assert answer.text == "Here's what I know."
        assert ("user", "should I switch majors?") in saved
        assert ("assistant", "Here's what I know.") in saved

    async def test_agent_failure_is_human(self, monkeypatch) -> None:
        class ExplodingLLM:
            pass

        patch_courses(monkeypatch, course())

        async def fake_history(session, settings, channel):
            return []

        monkeypatch.setattr(web_chat.memory, "load_history", fake_history)

        async def fake_system_prompt(session, settings, channel="web"):
            return "system"

        monkeypatch.setattr(web_chat, "build_system_prompt", fake_system_prompt)

        async def explode(*args, **kwargs):
            raise web_chat.LLMError("down")

        monkeypatch.setattr(web_chat, "run_agent", explode)

        answer = await answer_message(
            FakeSession(), make_settings(), ExplodingLLM(), "should I switch majors?"
        )
        assert answer.kind == "ai_error"


def httpx_response(payload: dict) -> object:
    import httpx

    return httpx.Response(200, json={"choices": [payload]})
