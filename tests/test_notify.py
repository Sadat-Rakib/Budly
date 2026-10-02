"""Notifier unit tests: escaping, splitting, quiz/missed logic, config, dry-run mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from canvasbuddy.config import Settings
from canvasbuddy.digest.builder import DigestContent, DigestItem
from canvasbuddy.models import Assignment
from canvasbuddy.notify.builders import _is_missed, _is_quiz_like, render_digest_slack
from canvasbuddy.notify.content import NotificationContent, Section
from canvasbuddy.notify.notifiers import (
    escape_slack,
    render_slack_chunks,
    render_telegram_chunks,
)
from canvasbuddy.slots import due_slots, parse_slot


def _settings(**overrides) -> Settings:
    defaults = {
        "canvas_base_url": "https://canvas.ualberta.ca",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "user_timezone": "America/Edmonton",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


def _assignment(**kw) -> Assignment:
    a = Assignment(canvas_id=1, course_id=1, name=kw.pop("name", "Homework 1"))
    for k, v in kw.items():
        setattr(a, k, v)
    return a


NOW = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)  # Sat 08:00 Edmonton (MDT)


class TestSlackEscape:
    def test_escapes_amp_lt_gt(self):
        assert escape_slack("a&b<c>d") == "a&amp;b&lt;c&gt;d"

    def test_render_chunks_escape_dynamic(self):
        c = NotificationContent(kind="review", title="T", sections=[Section("H", ["a&b"])])
        chunks = render_slack_chunks(c)
        assert "&amp;" in chunks[0]
        assert "&b" not in chunks[0].replace("&amp;", "")


class TestTelegramChunks:
    def test_splits_on_section_boundaries(self):
        secs = [Section(f"S{i}", [f"line {j}" for j in range(50)]) for i in range(10)]
        c = NotificationContent(kind="review", title="Big", sections=secs)
        chunks = render_telegram_chunks(c)
        assert len(chunks) > 1
        assert all(len(ch) <= 4000 for ch in chunks)

    def test_empty_sections_vanish(self):
        c = NotificationContent(
            kind="nudge", title="T", sections=[Section("Empty", []), Section("Full", ["x"])]
        )
        chunks = render_telegram_chunks(c)
        assert "Empty" not in chunks[0]
        assert "Full" in chunks[0]


class TestMissedLogic:
    def test_open_missed(self):
        a = _assignment(
            due_at=NOW - timedelta(days=2),
            has_submitted=False,
            score=None,
            workflow_state="published",
            submission_types=["online_text_entry"],
            lock_at=None,
        )
        assert _is_missed(a, NOW)

    def test_graded_not_missed(self):
        a = _assignment(
            due_at=NOW - timedelta(days=2),
            has_submitted=False,
            score=8.0,
            workflow_state="published",
            submission_types=["online_text_entry"],
        )
        assert not _is_missed(a, NOW)

    def test_on_paper_not_missed(self):
        a = _assignment(
            due_at=NOW - timedelta(days=1),
            has_submitted=False,
            score=None,
            workflow_state="published",
            submission_types=["on_paper"],
        )
        assert not _is_missed(a, NOW)

    def test_submitted_not_missed(self):
        a = _assignment(
            due_at=NOW - timedelta(days=1),
            has_submitted=True,
            score=None,
            workflow_state="published",
            submission_types=["online_text_entry"],
        )
        assert not _is_missed(a, NOW)


class TestQuizDetection:
    @pytest.mark.parametrize(
        "name,types,expected",
        [
            ("Midterm 1", ["online_text_entry"], True),
            ("Problem Set 3", ["online_text_entry"], False),
            ("Homework", ["online_quiz"], True),
            ("Final Exam", [], True),
            ("quiz 2", [], True),
        ],
    )
    def test_quiz(self, name, types, expected):
        a = _assignment(name=name, submission_types=types)
        assert _is_quiz_like(a) is expected


class TestDigestSlack:
    def test_does_not_parse_markdownv2(self):
        s = _settings()
        content = DigestContent(local_date=NOW)
        content.due_today = [
            DigestItem(
                course_code="DEMO",
                course_label="DEMO 101 · Intro",
                title="HW *bold* _x_",
                due_at=NOW + timedelta(hours=2),
                detail="20 pts",
            )
        ]
        out = render_digest_slack(content, s)
        # Slack renderer escapes but never emits MarkdownV2 backslashes
        assert "\\*" not in out
        assert "HW *bold*" in out or "HW" in out
        assert "*DUE TODAY*" in out or "DUE TODAY" in out


class TestConfigSlots:
    def test_bad_slot_fails_fast(self):
        with pytest.raises(ValueError):
            _settings(digest_slot="someday@07:00")

    def test_empty_disables(self):
        s = _settings(review_slot="")
        assert s.review_slot == ""
        assert all(slot.role != "review" for slot in s.active_slots())

    def test_defaults_parse(self):
        s = _settings()
        roles = {slot.role for slot in s.active_slots()}
        assert roles == {"digest", "nudge", "review", "checkin"}


class TestDryRunMapping:
    TZ = ZoneInfo("America/Edmonton")
    G = timedelta(minutes=180)

    def _roles(self, m, d, h, mi=0):
        slots = [
            parse_slot("digest", "daily@07:00"),
            parse_slot("nudge", "daily@20:00"),
            parse_slot("review", "sat@08:00"),
            parse_slot("checkin", "sat@15:00"),
        ]
        dt = datetime(2026, m, d, h, mi, tzinfo=self.TZ)
        return [s.role for s in due_slots(dt, slots, self.G)]

    def test_matrix(self):
        assert self._roles(9, 23, 7, 0) == ["digest"]  # Wed
        assert self._roles(9, 23, 20, 15) == ["nudge"]
        assert self._roles(9, 26, 7, 30) == []  # Sat
        assert self._roles(9, 26, 8, 0) == ["review"]
        assert self._roles(9, 26, 10, 45) == ["review"]
        assert self._roles(9, 26, 11, 0) == []
        assert self._roles(9, 26, 15, 15) == ["checkin"]
        assert self._roles(9, 26, 20, 15) == []
        assert self._roles(9, 27, 7, 0) == ["digest"]  # Sun
