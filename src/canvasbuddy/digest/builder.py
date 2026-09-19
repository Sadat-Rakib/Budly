"""Assemble the morning digest.

Editorial rules, in priority order:

1. **Empty sections vanish.** A short digest is the feature. A digest padded with
   "nothing due" headings trains you to stop reading it.
2. **Never hide a real deadline.** Where a heuristic is uncertain -- section matching in
   particular -- the ambiguous case shows everything rather than guessing. A duplicate
   line is a mild annoyance; a suppressed deadline is a missed assignment.
3. **Never repeat yourself.** Announcements already reported are tracked through
   ``digests.items`` so a slow news day does not re-run yesterday's headlines.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.config import Settings
from canvasbuddy.digest import render
from canvasbuddy.models import Announcement, Assignment, Course, Digest, Exam
from canvasbuddy.sync.courses import (
    SectionRef,
    enrolled_section_refs,
    name_stem,
    parse_section_name,
)

_HORIZON = timedelta(hours=72)


@dataclass
class DigestItem:
    course_code: str
    title: str
    due_at: datetime | None = None
    detail: str | None = None
    url: str | None = None
    #: "MGAB03 · Managerial Accounting". Printed once per course group rather than on
    #: every line, which is what makes the longer label affordable.
    course_label: str = ""


def group_by_course(items: list[DigestItem]) -> list[tuple[str, list[DigestItem]]]:
    """Group items by course, preserving the order they were sorted into.

    Sorting is chronological, so the first course to appear is the one with the
    earliest deadline -- the grouping keeps that priority rather than reordering
    alphabetically.
    """
    order: list[str] = []
    groups: dict[str, list[DigestItem]] = {}
    for item in items:
        key = item.course_label or item.course_code
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    return [(key, groups[key]) for key in order]


@dataclass
class DigestContent:
    """The assembled digest, before rendering."""

    local_date: datetime
    due_today: list[DigestItem] = field(default_factory=list)
    upcoming: list[DigestItem] = field(default_factory=list)
    undated: list[DigestItem] = field(default_factory=list)
    exams: list[DigestItem] = field(default_factory=list)
    announcements: list[DigestItem] = field(default_factory=list)
    graded: list[DigestItem] = field(default_factory=list)
    announcement_ids: list[int] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.due_today,
                self.upcoming,
                self.undated,
                self.exams,
                self.announcements,
                self.graded,
            )
        )


def _within_term(assignment: Assignment, course: Course) -> bool:
    """Reject deadlines from outside the course's own term.

    Instructors reuse course shells year to year, so a live 2026 shell can still carry
    assignments dated 2025. Those would otherwise surface as overdue work.
    """
    if assignment.due_at is None:
        return True
    start, end = course.term_start_at, course.term_end_at
    if start and assignment.due_at < start:
        return False
    if end and assignment.due_at > end:
        return False
    return True


def collapse_section_variants(
    items: list[tuple[Assignment, Course]],
) -> list[tuple[Assignment, Course, str | None]]:
    """Collapse per-section copies of one assignment down to the user's own.

    Some instructors create one assignment per section rather than using Canvas'
    override mechanism, so "Group Project - L01" through "L04" are four separate
    assignments, all visible to everyone, with nothing in the API marking which is
    yours. The only signal is the suffix in the name.

    Where exactly one variant matches an enrolled section, that one is returned with a
    label. Where zero or several match, *every* variant is returned unlabelled --
    guessing wrong here would hide real work.
    """
    groups: dict[tuple[int, str], list[tuple[Assignment, Course]]] = defaultdict(list)
    for assignment, course in items:
        groups[(course.id, name_stem(assignment.name))].append((assignment, course))

    result: list[tuple[Assignment, Course, str | None]] = []
    for members in groups.values():
        if len(members) == 1:
            result.append((members[0][0], members[0][1], None))
            continue

        course = members[0][1]
        enrolled = enrolled_section_refs(course.enrolled_sections)
        matched = [
            (assignment, course)
            for assignment, course in members
            if assignment.section_hint and _hint_ref(assignment.section_hint) in enrolled
        ]

        if len(matched) == 1:
            assignment, course = matched[0]
            result.append((assignment, course, _section_label(assignment, course)))
        else:
            result.extend((assignment, course, None) for assignment, course in members)

    return result


def _hint_ref(hint: str) -> SectionRef | None:
    return parse_section_name(hint)


def _section_label(assignment: Assignment, course: Course) -> str | None:
    """The enrolled section name matching this assignment's hint, for display."""
    ref = _hint_ref(assignment.section_hint or "")
    if ref is None:
        return None
    for name in course.enrolled_sections or []:
        if parse_section_name(name) == ref:
            return str(ref)
    return None


async def build_digest(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> DigestContent:
    tz: ZoneInfo = settings.tz
    now = (now or datetime.now(UTC)).astimezone(UTC)
    local_now = now.astimezone(tz)
    content = DigestContent(local_date=local_now)

    courses = {
        c.id: c
        for c in (
            await session.scalars(select(Course).where(Course.is_tracked, Course.is_active))
        ).all()
    }
    if not courses:
        return content

    assignments = (
        await session.scalars(
            select(Assignment).where(Assignment.course_id.in_(courses), ~Assignment.is_deleted)
        )
    ).all()

    dated: list[tuple[Assignment, Course]] = []
    for assignment in assignments:
        course = courses[assignment.course_id]
        if not _within_term(assignment, course):
            continue
        # Gradebook placeholders ("Midterm Score") are assignments to Canvas but are
        # not work anyone can do, so they never belong in a due list.
        if assignment.is_gradebook_column:
            continue
        dated.append((assignment, course))

    end_of_day = local_now.replace(hour=23, minute=59, second=59).astimezone(UTC)
    horizon = now + _HORIZON

    for assignment, course, label in collapse_section_variants(dated):
        if assignment.has_submitted:
            continue

        title = assignment.name
        if label:
            title = f"{title}  [{label}]"

        item = DigestItem(
            course_code=course.short_code or course.code,
            course_label=course.label,
            title=title,
            due_at=assignment.due_at,
            detail="not submitted",
            url=assignment.html_url,
        )

        if assignment.due_at is None:
            # Undated but graded work is real. MGHB02 alone carries 45 points of it.
            if assignment.points_possible:
                # "not submitted" is redundant here -- nothing undated has been
                # submitted. What is worth knowing is how much it is worth.
                points = assignment.points_possible
                item.detail = f"{points:g} pt" if points == 1 else f"{points:g} pts"
                content.undated.append(item)
        elif assignment.due_at < now:
            continue  # already past; the nudge job owns overdue work
        elif assignment.due_at <= end_of_day:
            content.due_today.append(item)
        elif assignment.due_at <= horizon:
            content.upcoming.append(item)

    content.due_today.sort(key=lambda i: i.due_at or now)
    content.upcoming.sort(key=lambda i: i.due_at or now)
    content.undated.sort(key=lambda i: (-(i.due_at is not None), i.course_code, i.title))

    await _add_exams(session, courses, content, now, settings)
    await _add_announcements(session, courses, content, now)

    if settings.show_grades_in_digest:
        _add_grades(assignments, courses, content, now)

    return content


async def _add_exams(
    session: AsyncSession,
    courses: dict[int, Course],
    content: DigestContent,
    now: datetime,
    settings: Settings,
) -> None:
    """Upcoming exams as a countdown.

    Only confirmed ones. An unconfirmed extraction is a guess a model made about a PDF,
    and a wrong exam date in a morning digest is worse than no exam date at all -- those
    wait for approval through the confirmation flow.
    """
    today = now.astimezone(settings.tz).date()
    rows = (
        await session.scalars(
            select(Exam)
            .where(
                Exam.course_id.in_(courses),
                Exam.confirmed_by_user,
                Exam.date.is_not(None),
                Exam.date >= today,
            )
            .order_by(Exam.date)
        )
    ).all()

    for row in rows:
        course = courses[row.course_id]
        days = (row.date - today).days
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"{days} days"
        # Built by hand rather than with strftime's %-d / %-I: those are POSIX-only and
        # raise on Windows, where this is developed.
        bits = [f"{row.date:%b} {row.date.day}"]
        if row.start_time:
            hour = row.start_time.hour % 12 or 12
            suffix = "am" if row.start_time.hour < 12 else "pm"
            bits.append(
                f"{hour}{suffix}"
                if row.start_time.minute == 0
                else f"{hour}:{row.start_time.minute:02d}{suffix}"
            )
        if row.location:
            bits.append(row.location)
        detail = [when, "(" + ", ".join(bits) + ")"]

        content.exams.append(
            DigestItem(
                course_code=course.short_code or course.code,
                course_label=course.label,
                title=row.title,
                detail=" ".join(detail),
            )
        )


async def _add_announcements(
    session: AsyncSession,
    courses: dict[int, Course],
    content: DigestContent,
    now: datetime,
) -> None:
    """Announcements not carried by any previous digest.

    Keyed on what was actually sent rather than on a time window, so a digest that was
    delayed or missed does not silently drop the news it should have carried.
    """
    already: set[int] = set()
    for row in (await session.scalars(select(Digest.items))).all():
        already.update((row or {}).get("announcement_ids", []))

    rows = (
        await session.scalars(
            select(Announcement)
            .where(
                Announcement.course_id.in_(courses),
                Announcement.posted_at.is_not(None),
                Announcement.posted_at >= now - timedelta(days=7),
            )
            .order_by(Announcement.posted_at.desc())
        )
    ).all()

    for row in rows:
        if row.canvas_id in already:
            continue
        course = courses[row.course_id]
        content.announcements.append(
            DigestItem(
                course_code=course.short_code or course.code,
                course_label=course.label,
                title=row.title,
                url=row.html_url,
            )
        )
        content.announcement_ids.append(row.canvas_id)


def _add_grades(
    assignments: list[Assignment],
    courses: dict[int, Course],
    content: DigestContent,
    now: datetime,
) -> None:
    cutoff = now - timedelta(days=1)
    for assignment in assignments:
        if assignment.score is None or assignment.graded_at is None:
            continue
        if assignment.graded_at < cutoff:
            continue
        course = courses[assignment.course_id]
        total = (
            f"{assignment.score:g}/{assignment.points_possible:g}"
            if assignment.points_possible
            else f"{assignment.score:g}"
        )
        content.graded.append(
            DigestItem(
                course_code=course.short_code or course.code,
                course_label=course.label,
                title=assignment.name,
                detail=total,
            )
        )


def render_digest(content: DigestContent, settings: Settings) -> str:
    """Render to Telegram MarkdownV2. Empty sections are omitted entirely."""
    tz = settings.tz
    esc = render.escape_md2
    today = content.local_date

    parts: list[str] = [
        render.bold(esc(f"📅 {render.format_header_date(today, tz)}")),
    ]

    def section(emoji: str, heading: str, items: list[DigestItem], *, with_due: bool) -> None:
        if not items:
            return
        parts.append("")
        parts.append(render.bold(esc(f"{emoji} {heading}")))
        for label, group in group_by_course(items):
            # The course header carries the full label so the lines under it can stay
            # short; repeating "MGAB03 · Managerial Accounting" per row would wrap
            # badly on a phone.
            parts.append(render.bold(esc(f"  {label}")))
            for item in group:
                bits: list[str] = []
                if with_due and item.due_at:
                    bits.append(render.format_due(item.due_at, tz, today=today))
                if item.detail:
                    bits.append(item.detail)
                line = f"    {item.title}"
                if bits:
                    line += " — " + " · ".join(bits)
                parts.append(esc(line))

    section("⚠️", "DUE TODAY", content.due_today, with_due=True)
    section("🔜", "NEXT 72 HOURS", content.upcoming, with_due=True)
    section("🎯", "EXAM COUNTDOWN", content.exams, with_due=False)
    section("📢", "NEW SINCE YESTERDAY", content.announcements, with_due=False)
    section("📊", "GRADED", content.graded, with_due=False)
    section("🗓", "NO DUE DATE SET", content.undated, with_due=False)

    if content.is_empty:
        parts.append("")
        parts.append(esc("Nothing due, nothing new. Enjoy it."))

    return "\n".join(parts)
