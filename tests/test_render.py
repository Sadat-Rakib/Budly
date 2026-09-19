"""MarkdownV2 escaping and date formatting.

Escaping is the single most likely cause of a failed send: Telegram rejects the entire
message with a 400 rather than degrading the formatting, and nearly every digest line
contains a course code, a time, or a date.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from canvasbuddy.digest.render import (
    escape_md2,
    format_day,
    format_due,
    format_header_date,
    format_time,
    link,
    strip_markdown,
)

TORONTO = ZoneInfo("America/Toronto")

#: Every character Telegram requires to be escaped in MarkdownV2.
SPECIALS = r"_*[]()~`>#+-=|{}.!"


class TestEscaping:
    @pytest.mark.parametrize("char", list(SPECIALS))
    def test_every_special_is_escaped(self, char: str) -> None:
        assert escape_md2(char) == "\\" + char

    def test_backslash_is_escaped_first(self) -> None:
        """Escaping the backslash after the others would double-escape them."""
        assert escape_md2("a\\b") == "a\\\\b"

    @pytest.mark.parametrize(
        "text",
        [
            "MGAB03H3 F LEC01",
            "Ch. 1 Quiz: Organizational Behaviour and Management",
            "Group Project - L04",
            "Reading response #1",
            "Midterm -35",
            "Personal Inventory Assessment: Are you a Type A Person?",
            "Price Theory: A Mathematical Approach",
        ],
        ids=lambda t: t[:24],
    )
    def test_real_course_data_survives(self, text: str) -> None:
        """Names taken verbatim from live Canvas data."""
        escaped = escape_md2(text)
        for i, char in enumerate(escaped):
            if char in SPECIALS:
                assert i > 0 and escaped[i - 1] == "\\", f"unescaped {char!r} in {escaped!r}"

    def test_plain_text_is_untouched(self) -> None:
        assert escape_md2("Groups") == "Groups"

    def test_link_url_is_not_over_escaped(self) -> None:
        """Escaping a URL the way body text is escaped breaks the link."""
        rendered = link("Quiz 3", "https://canvas.example.edu/courses/1/assignments/2?x=1")
        assert "https://canvas.example.edu/courses/1/assignments/2?x=1" in rendered
        assert rendered.startswith("[Quiz 3](")


class TestTimeFormatting:
    def test_utc_midnight_rolls_back_a_day_in_toronto(self) -> None:
        """The bug this catches is the one that misfiles deadlines by a day.

        An 11:59pm Eastern deadline is stored as 04:59Z the *next* morning. Bucketing
        on the UTC date puts it on the wrong calendar day.
        """
        due = datetime(2026, 11, 16, 4, 59, 59, tzinfo=UTC)
        assert format_time(due, TORONTO) == "11:59pm"
        assert format_day(due, TORONTO, today=datetime(2026, 11, 15, 12, 0, tzinfo=UTC)) == "today"

    def test_on_the_hour_drops_the_minutes(self) -> None:
        due = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)  # 5pm Toronto
        assert format_time(due, TORONTO) == "5pm"

    def test_noon_and_midnight(self) -> None:
        assert format_time(datetime(2026, 10, 6, 16, 0, tzinfo=UTC), TORONTO) == "12pm"
        assert format_time(datetime(2026, 10, 6, 4, 0, tzinfo=UTC), TORONTO) == "12am"

    def test_relative_day_labels(self) -> None:
        today = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        assert format_day(datetime(2026, 9, 9, 20, 0, tzinfo=UTC), TORONTO, today=today) == "today"
        assert (
            format_day(datetime(2026, 9, 10, 20, 0, tzinfo=UTC), TORONTO, today=today) == "tomorrow"
        )
        assert format_day(datetime(2026, 9, 11, 20, 0, tzinfo=UTC), TORONTO, today=today) == "Fri"
        assert (
            format_day(datetime(2026, 11, 16, 4, 0, tzinfo=UTC), TORONTO, today=today) == "Nov 15"
        )

    def test_due_combines_day_and_time(self) -> None:
        today = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        due = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)
        assert format_due(due, TORONTO, today=today) == "Fri 5pm"

    def test_header_date(self) -> None:
        assert format_header_date(datetime(2026, 9, 9, 12, 0, tzinfo=UTC), TORONTO) == (
            "Wednesday, Sep 9"
        )

    def test_dst_boundary_is_handled_by_zoneinfo(self) -> None:
        """November 1 2026 is the US/Canada DST fallback; the offset changes, the code does not."""
        before = datetime(2026, 10, 30, 23, 59, tzinfo=UTC)  # EDT, UTC-4
        after = datetime(2026, 11, 3, 23, 59, tzinfo=UTC)  # EST, UTC-5
        assert format_time(before, TORONTO) == "7:59pm"
        assert format_time(after, TORONTO) == "6:59pm"


class TestStripMarkdown:
    """Chat replies are sent without a parse mode, so any Markdown arrives literally."""

    def test_bold_becomes_plain(self) -> None:
        assert strip_markdown("You have **two** things due") == "You have two things due"

    def test_italic_becomes_plain(self) -> None:
        assert strip_markdown("that's *probably* fine") == "that's probably fine"

    def test_bold_italic_becomes_plain(self) -> None:
        assert strip_markdown("***urgent***") == "urgent"

    def test_several_spans_on_one_line(self) -> None:
        assert strip_markdown("**MGAB03** and **PHLB18**") == "MGAB03 and PHLB18"

    def test_bullets_become_real_bullets(self) -> None:
        assert strip_markdown("- one\n- two") == "• one\n• two"
        assert strip_markdown("* one\n* two") == "• one\n• two"

    def test_headings_are_dropped(self) -> None:
        assert strip_markdown("## Due today\nnothing") == "Due today\nnothing"

    def test_backticks_go(self) -> None:
        assert strip_markdown("run `/today` for that") == "run /today for that"

    def test_underscores_survive(self) -> None:
        """Field and tool names are full of them; a pairwise strip would mangle these."""
        assert strip_markdown("list_upcoming and points_possible") == (
            "list_upcoming and points_possible"
        )
        assert strip_markdown("a_b_c_d") == "a_b_c_d"

    def test_plain_text_is_untouched(self) -> None:
        text = "Reading response #1 is due friday 11:59pm. Nothing else this week."
        assert strip_markdown(text) == text

    def test_a_lone_asterisk_is_left_alone(self) -> None:
        assert strip_markdown("2 * 3 = 6") == "2 * 3 = 6"

    def test_multiline_bold_span(self) -> None:
        assert strip_markdown("**due\nsoon**") == "due\nsoon"

    def test_a_realistic_reply(self) -> None:
        raw = (
            "## This week\n"
            "You've got **one** thing due:\n"
            "- **Reading response #1** — friday 11:59pm (PHLB18)\n\n"
            "Nothing else until *oct 1*."
        )
        assert strip_markdown(raw) == (
            "This week\n"
            "You've got one thing due:\n"
            "• Reading response #1 — friday 11:59pm (PHLB18)\n\n"
            "Nothing else until oct 1."
        )
