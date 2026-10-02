"""One full sync pass over Canvas.

Order matters: courses must be synced before announcements, because the announcements
endpoint takes ``context_codes[]`` and those come from the course list.

The assignment store is built from two endpoints, neither of which is sufficient alone:

* ``/courses/:id/assignments`` is the canonical list. It is complete, and it is the
  only source for assignments with no due date -- which are real graded work, not
  noise -- and for gradebook-only columns carrying scores.
* ``/planner/items`` is an overlay. Canvas resolves section overrides server-side
  there, so its dates are the ones that genuinely apply to this student, but it drops
  every item with a null due date.

Writing from the first and flagging from the second gets both properties.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.canvas import schemas
from canvasbuddy.canvas.client import CanvasClient
from canvasbuddy.config import Settings
from canvasbuddy.models import Announcement, Assignment, Course, Event, EventType
from canvasbuddy.sync.courses import (
    course_title,
    is_tracked_term,
    parse_section_hint,
    short_course_code,
)
from canvasbuddy.sync.diff import AssignmentState, PendingEvent, detect_removals, diff_assignment

log = logging.getLogger(__name__)

#: Re-fetch a day of already-seen announcements each pass. Cheap, and it closes the
#: gap where something posted during a sync would otherwise fall between two windows.
_ANNOUNCEMENT_OVERLAP = timedelta(hours=24)
#: How far back and forward the planner overlay reaches when a term has no dates.
_PLANNER_FALLBACK = timedelta(days=180)


@dataclass
class SyncReport:
    """What one pass did. Printed by ``canvasbuddy sync`` and used by the tests."""

    courses_seen: int = 0
    courses_tracked: int = 0
    assignments_upserted: int = 0
    assignments_on_planner: int = 0
    announcements_upserted: int = 0
    events: list[PendingEvent] = field(default_factory=list)
    bootstrapped_courses: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"courses:       {self.courses_tracked} tracked / {self.courses_seen} seen",
            f"assignments:   {self.assignments_upserted} upserted "
            f"({self.assignments_on_planner} on planner, "
            f"{self.assignments_upserted - self.assignments_on_planner} off)",
            f"announcements: {self.announcements_upserted} upserted",
            f"events:        {len(self.events)}",
        ]
        if self.bootstrapped_courses:
            lines.append(
                "bootstrapped:  "
                + ", ".join(self.bootstrapped_courses)
                + "  (events suppressed on first sight)"
            )
        return "\n".join(lines)


async def sync_all(
    session: AsyncSession,
    client: CanvasClient,
    settings: Settings,
    *,
    dry_run: bool = False,
) -> SyncReport:
    report = SyncReport()

    courses = await _sync_courses(session, client, settings, report)
    tracked = [c for c in courses if c.is_tracked]
    report.courses_tracked = len(tracked)

    if not tracked:
        log.warning(
            "No courses matched term %r. Run `canvasbuddy courses` to see what Canvas returned.",
            settings.canvas_term,
        )
        return report

    await session.flush()

    if getattr(client, "mock", False) and all(c.bootstrapped_at is not None for c in tracked):
        # The fixture Canvas tells its change story (moved deadline, new work, news)
        # on every sync after the bootstrap pass, so demos exercise change detection.
        client.apply_scripted_changes()

    planner_ids = await _fetch_planner_ids(client, tracked)

    for course in tracked:
        await _sync_course_assignments(session, client, course, planner_ids, report)

    await _sync_announcements(session, client, tracked, report)

    # Stamp bootstrap only after a course's first pass has fully succeeded, so a crash
    # mid-pass does not silently swallow the events of the retry.
    now = datetime.now(UTC)
    for course in tracked:
        if course.bootstrapped_at is None:
            course.bootstrapped_at = now
            report.bootstrapped_courses.append(course.code)

    if not dry_run:
        for pending in report.events:
            session.add(
                Event(
                    type=pending.type,
                    entity_type=pending.entity_type,
                    entity_id=pending.canvas_id,
                    payload=pending.payload,
                )
            )
    else:
        await session.rollback()

    return report


async def _sync_courses(
    session: AsyncSession,
    client: CanvasClient,
    settings: Settings,
    report: SyncReport,
) -> list[Course]:
    payload = await client.get_courses()
    existing = {c.canvas_id: c for c in (await session.scalars(select(Course))).all()}
    result: list[Course] = []

    for raw in payload:
        parsed = schemas.Course.model_validate(raw)
        report.courses_seen += 1

        course = existing.get(parsed.id)
        if course is None:
            course = Course(canvas_id=parsed.id)
            session.add(course)

        course.code = parsed.display_code
        course.short_code = short_course_code(parsed.course_code or parsed.display_code)
        course.name = parsed.name
        # Recomputed every sync so a fixed parser improves existing rows, but never
        # touches `nickname` -- that is the user's, and overrides this.
        course.title = course_title(parsed.name, parsed.course_code)
        course.is_active = True
        if parsed.syllabus_body:
            course.syllabus_html = parsed.syllabus_body
            course.syllabus_synced_at = datetime.now(UTC)

        if parsed.term:
            course.term_id = parsed.term.id
            course.term_name = parsed.term.name
            course.term_start_at = parsed.term.start_at
            course.term_end_at = parsed.term.end_at

        # A manual `canvasbuddy track` must survive later syncs, so the term filter only
        # applies to courses that have never been classified.
        if course.bootstrapped_at is None:
            course.is_tracked = is_tracked_term(course.term_name, settings.canvas_term)

        result.append(course)

    for canvas_id, course in existing.items():
        if canvas_id not in {c.canvas_id for c in result}:
            # Term rollover: archive rather than delete, so history survives.
            course.is_active = False

    for course in result:
        if course.is_tracked and not course.enrolled_sections:
            course.enrolled_sections = await _fetch_enrolled_sections(client, course.canvas_id)

    return result


async def _fetch_enrolled_sections(client: CanvasClient, course_canvas_id: int) -> list[str]:
    """Names of the sections this user is enrolled in for one course.

    Two calls, because enrolments give section *ids* and only the sections endpoint
    gives their names.
    """
    enrollments = [
        schemas.Enrollment.model_validate(e) for e in await client.get_enrollments(course_canvas_id)
    ]
    section_ids = {e.course_section_id for e in enrollments if e.course_section_id}
    if not section_ids:
        return []
    sections = [
        schemas.Section.model_validate(s) for s in await client.get_sections(course_canvas_id)
    ]
    return [s.name for s in sections if s.id in section_ids]


async def _fetch_planner_ids(client: CanvasClient, tracked: list[Course]) -> set[int]:
    """One planner call across every tracked course.

    Returns the set of assignment ids the planner knows about, which is the overlay
    applied to the canonical assignment list.
    """
    starts = [c.term_start_at for c in tracked if c.term_start_at]
    ends = [c.term_end_at for c in tracked if c.term_end_at]
    now = datetime.now(UTC)
    start = min(starts) if starts else now - _PLANNER_FALLBACK
    end = max(ends) if ends else now + _PLANNER_FALLBACK

    context_codes = [f"course_{c.canvas_id}" for c in tracked]
    raw = await client.get_planner_items(start.date(), end.date(), context_codes)

    ids: set[int] = set()
    for row in raw:
        item = schemas.PlannerItem.model_validate(row)
        if item.plannable_type in {"assignment", "quiz", "discussion_topic"} and item.plannable_id:
            ids.add(item.plannable_id)
    return ids


async def _sync_course_assignments(
    session: AsyncSession,
    client: CanvasClient,
    course: Course,
    planner_ids: set[int],
    report: SyncReport,
) -> None:
    payload = await client.get_assignments(course.canvas_id)
    bootstrapped = course.bootstrapped_at is not None

    stored_rows = (
        await session.scalars(select(Assignment).where(Assignment.course_id == course.id))
    ).all()
    stored = {a.canvas_id: a for a in stored_rows}
    stored_states = {
        canvas_id: AssignmentState(
            canvas_id=canvas_id,
            name=row.name,
            due_at=row.due_at,
            points_possible=row.points_possible,
            workflow_state=row.workflow_state,
            score=row.score,
            has_submitted=row.has_submitted,
        )
        for canvas_id, row in stored.items()
        if not row.is_deleted
    }

    incoming_ids: set[int] = set()

    for raw in payload:
        parsed = schemas.Assignment.model_validate(raw)
        incoming_ids.add(parsed.id)
        submission = parsed.submission

        incoming_state = AssignmentState(
            canvas_id=parsed.id,
            name=parsed.name,
            due_at=parsed.due_at,
            points_possible=parsed.points_possible,
            workflow_state=parsed.workflow_state,
            score=submission.score if submission else None,
            has_submitted=parsed.has_submitted,
        )

        report.events.extend(
            diff_assignment(stored_states.get(parsed.id), incoming_state, bootstrapped=bootstrapped)
        )

        row = stored.get(parsed.id)
        if row is None:
            row = Assignment(canvas_id=parsed.id, course_id=course.id)
            session.add(row)

        hint = parse_section_hint(parsed.name)

        row.name = parsed.name
        row.description_html = parsed.description
        row.due_at = parsed.due_at
        row.unlock_at = parsed.unlock_at
        row.lock_at = parsed.lock_at
        row.points_possible = parsed.points_possible
        row.submission_types = parsed.submission_types
        row.html_url = parsed.html_url
        row.workflow_state = parsed.workflow_state
        row.has_submitted = parsed.has_submitted
        row.submitted_at = submission.submitted_at if submission else None
        row.score = submission.score if submission else None
        row.grade = submission.grade if submission else None
        row.graded_at = submission.graded_at if submission else None
        row.section_hint = str(hint) if hint else None
        row.on_planner = parsed.id in planner_ids
        row.is_deleted = False
        row.last_changed_at = datetime.now(UTC)

        report.assignments_upserted += 1
        if row.on_planner:
            report.assignments_on_planner += 1

    report.events.extend(detect_removals(stored_states, incoming_ids, bootstrapped=bootstrapped))
    for canvas_id, row in stored.items():
        if canvas_id not in incoming_ids:
            row.is_deleted = True


async def _sync_announcements(
    session: AsyncSession,
    client: CanvasClient,
    tracked: list[Course],
    report: SyncReport,
) -> None:
    by_canvas_id = {c.canvas_id: c for c in tracked}
    context_codes = [f"course_{c.canvas_id}" for c in tracked]

    latest = await session.scalar(
        select(Announcement.posted_at).order_by(Announcement.posted_at.desc()).limit(1)
    )
    now = datetime.now(UTC)
    start = (latest - _ANNOUNCEMENT_OVERLAP) if latest else (now - timedelta(days=30))

    payload = await client.get_announcements(
        context_codes, start.date(), (now + timedelta(days=1)).date()
    )
    existing = {a.canvas_id: a for a in (await session.scalars(select(Announcement))).all()}

    for raw in payload:
        parsed = schemas.Announcement.model_validate(raw)
        course = by_canvas_id.get(parsed.course_canvas_id or -1)
        if course is None:
            continue

        row = existing.get(parsed.id)
        is_new = row is None
        if row is None:
            row = Announcement(canvas_id=parsed.id, course_id=course.id)
            session.add(row)

        row.title = parsed.title
        row.body_html = parsed.message
        row.body_text = parsed.body_text
        row.posted_at = parsed.posted_at
        row.author_name = parsed.user_name
        row.html_url = parsed.html_url

        report.announcements_upserted += 1

        if is_new and course.bootstrapped_at is not None:
            report.events.append(
                PendingEvent(
                    type=EventType.new_announcement,
                    entity_type="announcement",
                    canvas_id=parsed.id,
                    payload={
                        "title": parsed.title,
                        "course_code": course.code,
                        "posted_at": parsed.posted_at.isoformat() if parsed.posted_at else None,
                        "html_url": parsed.html_url,
                    },
                )
            )
