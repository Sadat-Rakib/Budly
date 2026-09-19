"""Digest assembly and rendering.

Uses unsaved ORM instances rather than a database: the rules under test are decisions
about presentation, and binding them to a live Postgres would test the driver instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

from canvasbuddy.config import Settings
from canvasbuddy.digest.builder import (
    DigestContent,
    DigestItem,
    collapse_section_variants,
    group_by_course,
    render_digest,
)
from canvasbuddy.models import Assignment, Course

NOW = datetime(2026, 9, 9, 11, 0, tzinfo=UTC)


def settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "user_timezone": "America/Toronto",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def course(**overrides: object) -> Course:
    c = Course(canvas_id=455965, code="MGAB03H3", name="Introductory Management Accounting")
    c.id = 1
    c.enrolled_sections = ["MGAB03H3-F-LEC04-20269", "MGAB03H3-F-TUT0002-20269"]
    for key, value in overrides.items():
        setattr(c, key, value)
    return c


def assignment(name: str, hint: str | None, canvas_id: int = 1) -> Assignment:
    a = Assignment(canvas_id=canvas_id, course_id=1, name=name)
    a.section_hint = hint
    return a


class TestSectionCollapse:
    def test_the_matching_section_wins(self) -> None:
        """Live case: MGAB03 has one assignment per lecture section, no overrides.

        The student is in LEC04, so only "Group Project - L04" is theirs.
        """
        c = course()
        items = [
            (assignment(f"Group Project - L0{i}", f"LEC0{i}", canvas_id=i), c) for i in range(1, 5)
        ]

        result = collapse_section_variants(items)

        assert len(result) == 1
        picked, _, label = result[0]
        assert picked.name == "Group Project - L04"
        assert label == "LEC04"

    def test_no_match_shows_everything(self) -> None:
        """Guessing wrong would hide real work, so ambiguity shows all variants."""
        c = course(enrolled_sections=["MGAB03H3-F-LEC09-20269"])
        items = [
            (assignment(f"Group Project - L0{i}", f"LEC0{i}", canvas_id=i), c) for i in range(1, 5)
        ]

        result = collapse_section_variants(items)

        assert len(result) == 4
        assert all(label is None for _, _, label in result)

    def test_unknown_enrolment_shows_everything(self) -> None:
        c = course(enrolled_sections=None)
        items = [
            (assignment(f"Group Project - L0{i}", f"LEC0{i}", canvas_id=i), c) for i in range(1, 3)
        ]
        assert len(collapse_section_variants(items)) == 2

    def test_unrelated_assignments_are_untouched(self) -> None:
        c = course()
        items = [
            (assignment("Reading response #1", None, canvas_id=10), c),
            (assignment("Reading response #2", None, canvas_id=11), c),
        ]

        result = collapse_section_variants(items)

        assert len(result) == 2
        assert all(label is None for _, _, label in result)

    def test_a_lone_sectioned_assignment_is_not_labelled(self) -> None:
        """With nothing to disambiguate against, the label would be noise."""
        c = course()
        result = collapse_section_variants([(assignment("Essay - L04", "LEC04"), c)])
        assert result[0][2] is None


class TestRendering:
    def test_empty_sections_are_omitted(self) -> None:
        content = DigestContent(local_date=NOW)
        content.due_today.append(
            DigestItem(
                course_code="MGAB03", title="Problem Set 2", due_at=NOW, detail="not submitted"
            )
        )

        body = render_digest(content, settings())

        assert "DUE TODAY" in body
        assert "NEXT 72 HOURS" not in body
        assert "GRADED" not in body
        assert "NEW SINCE YESTERDAY" not in body

    def test_a_quiet_day_says_so(self) -> None:
        body = render_digest(DigestContent(local_date=NOW), settings())
        assert "Nothing due" in body

    def test_output_is_fully_escaped(self) -> None:
        """Any unescaped special character makes Telegram reject the whole message."""
        content = DigestContent(local_date=NOW)
        content.due_today.append(
            DigestItem(
                course_code="MGHB02",
                title="Ch. 1 Quiz: Organizational Behaviour (Part 2)",
                due_at=NOW,
                detail="not submitted",
            )
        )
        content.announcements.append(
            DigestItem(course_code="MGAB03", title="Class (September 15) - Lecture 03")
        )

        body = render_digest(content, settings())

        for i, char in enumerate(body):
            if char in r"_[]()~`>#+-=|{}.!":
                assert body[i - 1] == "\\", (
                    f"unescaped {char!r} at {i}: {body[max(0, i - 30) : i + 5]!r}"
                )

    def test_section_label_reaches_the_output(self) -> None:
        content = DigestContent(local_date=NOW)
        content.due_today.append(
            DigestItem(course_code="MGAB03", title="Group Project  [LEC04]", due_at=NOW)
        )
        assert "LEC04" in render_digest(content, settings())

    def test_undated_work_gets_its_own_section(self) -> None:
        """MGHB02 alone carries 45 points of published, undated quizzes."""
        content = DigestContent(local_date=NOW)
        content.undated.append(
            DigestItem(course_code="MGHB02", title="Ch. 13 Quiz: Conflict and Stress")
        )

        body = render_digest(content, settings())

        assert "NO DUE DATE SET" in body
        assert "Ch\\. 13 Quiz" in body


class TestCourseGrouping:
    def test_items_group_under_one_course_header(self) -> None:
        content = DigestContent(local_date=NOW)
        for title in ("Ch. 13 Quiz", "Ch. 14 Quiz"):
            content.undated.append(
                DigestItem(
                    course_code="MGHB02",
                    course_label="MGHB02 · Managing People and Groups",
                    title=title,
                    detail="15 pts",
                )
            )

        body = render_digest(content, settings())

        # The label is printed once, not per row -- repeating it would wrap badly.
        assert body.count("Managing People") == 1
        assert r"Ch\. 13 Quiz" in body
        assert r"Ch\. 14 Quiz" in body

    def test_grouping_preserves_deadline_order(self) -> None:
        """Courses appear in order of their earliest deadline, not alphabetically."""
        content = DigestContent(local_date=NOW)
        content.due_today = [
            DigestItem(course_code="PHLB18", course_label="PHLB18 · Ethics", title="Response"),
            DigestItem(course_code="MGAB03", course_label="MGAB03 · Accounting", title="Project"),
        ]

        body = render_digest(content, settings())

        assert body.index("Ethics") < body.index("Accounting")

    def test_interleaved_courses_collapse_into_groups(self) -> None:
        items = [
            DigestItem(course_code="A", course_label="A · One", title="a1"),
            DigestItem(course_code="B", course_label="B · Two", title="b1"),
            DigestItem(course_code="A", course_label="A · One", title="a2"),
        ]
        grouped = group_by_course(items)
        assert [label for label, _ in grouped] == ["A · One", "B · Two"]
        assert [i.title for i in grouped[0][1]] == ["a1", "a2"]

    def test_label_falls_back_to_code_when_unset(self) -> None:
        grouped = group_by_course([DigestItem(course_code="MGAB03", title="x")])
        assert grouped[0][0] == "MGAB03"


class TestCourseLabel:
    def test_nickname_wins_over_parsed_title(self) -> None:
        c = course()
        c.short_code, c.title, c.nickname = "MGAB03", "Introductory Management Accounting", None
        assert c.label == "MGAB03 · Introductory Management Accounting"
        c.nickname = "Managerial Accounting"
        assert c.label == "MGAB03 · Managerial Accounting"

    def test_bare_code_when_there_is_no_name(self) -> None:
        c = course()
        c.short_code, c.title, c.nickname = "MGAB03", None, None
        assert c.label == "MGAB03"
