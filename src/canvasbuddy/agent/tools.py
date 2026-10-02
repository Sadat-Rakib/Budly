"""Typed tools over the database.

The PRD rules out text-to-SQL, and that call holds up. The strongest prior art in this
space hands the model an ``execute_sql`` tool and lets it explore the schema and
self-correct from error messages -- a good pattern for an unknown database, and the wrong
one here. This schema is small, fixed, and already understood, so writing the queries
ourselves produces better answers *and* removes the injection surface rather than
mitigating it. There is no model-authored SQL, so there is nothing to sandbox: the
enforceable boundary is that the model can only call the five functions below.

Each tool returns plain JSON-serialisable data. Dates are rendered in the user's timezone
at this boundary, because the model reasons about "Friday" far better than about
``2026-09-11T21:00:00Z``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.config import Settings
from canvasbuddy.digest.builder import collapse_section_variants
from canvasbuddy.models import Announcement, Assignment, Contact, Course, Exam, File, ManualItem

ToolFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# --------------------------------------------------------------------------- helpers


async def _tracked_courses(session: AsyncSession) -> list[Course]:
    return list(
        (await session.scalars(select(Course).where(Course.is_tracked, Course.is_active))).all()
    )


def _match_course(courses: list[Course], code: str | None) -> Course | None:
    """Resolve a course by whatever form the user typed.

    People say "MGAB03"; Canvas says "MGAB03H3 F LEC01 20269". Matching is loose on
    purpose -- a wrong lookup here surfaces as "I don't know about that course", which is
    a worse failure than being generous.
    """
    if not code:
        return None
    needle = code.strip().lower()
    for course in courses:
        candidates = {
            (course.short_code or "").lower(),
            (course.code or "").lower(),
            (course.nickname or "").lower(),
        }
        if needle in candidates:
            return course
    for course in courses:
        haystack = f"{course.short_code} {course.code} {course.nickname or ''} {course.title or ''}"
        if needle in haystack.lower():
            return course
    return None


def _local(dt: datetime | None, settings: Settings) -> str | None:
    return dt.astimezone(settings.tz).isoformat(timespec="minutes") if dt else None


def _assignment_row(assignment: Assignment, course: Course, settings: Settings) -> dict:
    return {
        "course": course.short_code or course.code,
        "course_name": course.nickname or course.title,
        "title": assignment.name,
        "due_at": _local(assignment.due_at, settings),
        "points_possible": assignment.points_possible,
        "submitted": assignment.has_submitted,
        "score": assignment.score,
        "url": assignment.html_url,
    }


async def _live_assignments(
    session: AsyncSession, settings: Settings, courses: list[Course]
) -> list[tuple[Assignment, Course, str | None]]:
    """Every assignment worth talking about, with the digest's own filters applied.

    Reuses the term-window, gradebook-column and section-collapse rules so the chat agent
    and the digest can never disagree about what counts as real work. Collapsing happens
    here rather than per-tool because *every* consumer wants the user's own copy: without
    it, MGAB03's four per-section Group Projects make the course look like 400 points of
    work instead of 100.
    """
    by_id = {c.id: c for c in courses}
    rows = (
        await session.scalars(
            select(Assignment).where(Assignment.course_id.in_(by_id), ~Assignment.is_deleted)
        )
    ).all()

    kept: list[tuple[Assignment, Course]] = []
    for assignment in rows:
        course = by_id[assignment.course_id]
        if assignment.is_gradebook_column:
            continue
        if assignment.due_at and course.term_start_at and assignment.due_at < course.term_start_at:
            continue
        if assignment.due_at and course.term_end_at and assignment.due_at > course.term_end_at:
            continue
        kept.append((assignment, course))

    return collapse_section_variants(kept)


# ----------------------------------------------------------------------------- tools


async def list_upcoming(
    session: AsyncSession,
    settings: Settings,
    days: int = 7,
    course_code: str | None = None,
) -> dict:
    """Assignments due within a window."""
    courses = await _tracked_courses(session)
    target = _match_course(courses, course_code)
    if course_code and target is None:
        return {"error": f"No tracked course matching {course_code!r}."}

    scope = [target] if target else courses
    collapsed = await _live_assignments(session, settings, scope)

    now = datetime.now(UTC)
    horizon = now + timedelta(days=days)

    due: list[dict] = []
    undated: list[dict] = []
    for assignment, course, label in collapsed:
        row = _assignment_row(assignment, course, settings)
        if label:
            row["your_section"] = label
        if assignment.due_at is None:
            if assignment.points_possible:
                undated.append(row)
        elif now <= assignment.due_at <= horizon:
            due.append(row)

    due.sort(key=lambda r: r["due_at"] or "")
    return {
        "window_days": days,
        "today": datetime.now(settings.tz).date().isoformat(),
        "due": due,
        "no_due_date_set": undated,
    }


async def get_course(session: AsyncSession, settings: Settings, code: str) -> dict:
    """One course: identity, enrolment, and its assessment load."""
    courses = await _tracked_courses(session)
    course = _match_course(courses, code)
    if course is None:
        return {
            "error": f"No tracked course matching {code!r}.",
            "available": [c.short_code or c.code for c in courses],
        }

    pairs = await _live_assignments(session, settings, [course])
    total_points = sum(a.points_possible or 0 for a, _, _ in pairs)
    return {
        "course": course.short_code or course.code,
        "name": course.nickname or course.title,
        "canvas_name": course.name,
        "term": course.term_name,
        "your_sections": course.enrolled_sections or [],
        "assignment_count": len(pairs),
        "total_points": total_points,
        "has_syllabus_text": bool(course.syllabus_html),
    }


async def search_announcements(
    session: AsyncSession,
    settings: Settings,
    query: str,
    course_code: str | None = None,
    since: str | None = None,
) -> dict:
    """Keyword search over announcement titles and bodies.

    Keyword only. Plain substring matching is plenty at one student's volume of
    announcements, and it needs no vector database.
    """
    courses = await _tracked_courses(session)
    target = _match_course(courses, course_code)
    if course_code and target is None:
        return {"error": f"No tracked course matching {course_code!r}."}

    by_id = {c.id: c for c in ([target] if target else courses)}
    statement = select(Announcement).where(Announcement.course_id.in_(by_id))

    if query.strip():
        pattern = f"%{query.strip()}%"
        statement = statement.where(
            or_(Announcement.title.ilike(pattern), Announcement.body_text.ilike(pattern))
        )
    if since:
        try:
            statement = statement.where(
                Announcement.posted_at >= datetime.fromisoformat(since).replace(tzinfo=UTC)
            )
        except ValueError:
            return {"error": f"Could not read {since!r} as a date. Use YYYY-MM-DD."}

    rows = (
        await session.scalars(statement.order_by(Announcement.posted_at.desc()).limit(15))
    ).all()

    return {
        "query": query,
        "matches": [
            {
                "course": by_id[row.course_id].short_code or by_id[row.course_id].code,
                "title": row.title,
                "posted_at": _local(row.posted_at, settings),
                # Truncated because a full announcement body can be very long and the
                # model rarely needs more than the gist plus the link.
                "excerpt": (row.body_text or "")[:600],
                "url": row.html_url,
            }
            for row in rows
        ],
    }


async def get_grades(
    session: AsyncSession, settings: Settings, course_code: str | None = None
) -> dict:
    """Scores received so far, and what they add up to."""
    courses = await _tracked_courses(session)
    target = _match_course(courses, course_code)
    if course_code and target is None:
        return {"error": f"No tracked course matching {course_code!r}."}

    scope = [target] if target else courses
    by_id = {c.id: c for c in scope}
    rows = (
        await session.scalars(
            select(Assignment).where(
                Assignment.course_id.in_(by_id),
                ~Assignment.is_deleted,
                Assignment.score.is_not(None),
            )
        )
    ).all()

    graded: list[dict] = []
    earned = 0.0
    out_of = 0.0
    for assignment in rows:
        course = by_id[assignment.course_id]
        graded.append(
            {
                "course": course.short_code or course.code,
                "title": assignment.name,
                "score": assignment.score,
                "points_possible": assignment.points_possible,
                "graded_at": _local(assignment.graded_at, settings),
            }
        )
        if assignment.points_possible:
            earned += assignment.score or 0
            out_of += assignment.points_possible

    return {
        "graded": graded,
        "points_earned": round(earned, 2),
        "points_out_of": round(out_of, 2),
        "percent": round(100 * earned / out_of, 1) if out_of else None,
        # Said explicitly because "nothing graded yet" and "you scored zero" are very
        # different facts, and a model shown an empty list will sometimes conflate them.
        "note": "Nothing has been graded yet." if not graded else None,
    }


async def list_overdue(
    session: AsyncSession, settings: Settings, course_code: str | None = None
) -> dict:
    """Unsubmitted work whose due date has already passed.

    Shares the nudge's definition of missed, so chat and digest can never disagree
    about what counts: published, submittable, no score, and past due.
    """
    from canvasbuddy.notify.builders import _is_missed

    courses = await _tracked_courses(session)
    target = _match_course(courses, course_code)
    if course_code and target is None:
        return {"error": f"No tracked course matching {course_code!r}."}

    scope = [target] if target else courses
    collapsed = await _live_assignments(session, settings, scope)

    now = datetime.now(UTC)
    overdue = []
    for assignment, course, label in collapsed:
        if not _is_missed(assignment, now):
            continue
        row = _assignment_row(assignment, course, settings)
        if label:
            row["your_section"] = label
        overdue.append(row)

    overdue.sort(key=lambda r: r["due_at"] or "")
    return {"overdue": overdue, "count": len(overdue)}


async def get_changes_since(
    session: AsyncSession, settings: Settings, since: str | None = None
) -> dict:
    """Everything that changed since a moment: events plus fresh announcements.

    This is the change-detection store being read back out. Event rows are written by
    the sync's diff engine; announcements are listed directly because their arrival is
    itself the change. Points tweaks and state flips are deliberately excluded -- they
    are metadata noise in an answer to "what's new".
    """
    courses = await _tracked_courses(session)
    by_id = {c.id: c for c in courses}

    now = datetime.now(UTC)
    if since:
        try:
            start = datetime.fromisoformat(since).replace(tzinfo=UTC)
        except ValueError:
            return {"error": f"Could not read {since!r} as a date. Use YYYY-MM-DD."}
    else:
        start = now - timedelta(days=1)

    from canvasbuddy.models import Event, EventType

    interesting = {
        EventType.new_assignment,
        EventType.due_date_changed,
        EventType.assignment_removed,
        EventType.new_announcement,
    }
    rows = (
        await session.scalars(
            select(Event)
            .where(Event.created_at >= start, Event.type.in_(interesting))
            .order_by(Event.created_at.desc())
            .limit(30)
        )
    ).all()

    changes: list[dict] = []
    for row in rows:
        course = None
        if row.payload.get("course_code"):
            course = _match_course(courses, row.payload["course_code"])
        elif row.entity_type == "assignment":
            assignment = await session.scalar(
                select(Assignment).where(Assignment.canvas_id == row.entity_id)
            )
            if assignment is not None:
                course = by_id.get(assignment.course_id)

        entry: dict[str, Any] = {
            "type": row.type.value if row.type else str(row.type),
            "what": row.payload.get("name") or row.payload.get("title"),
            "course": (course.short_code or course.code) if course else None,
            "when": _local(row.created_at, settings),
        }
        if row.type == EventType.due_date_changed:
            entry["changed"] = {
                "from": _local(
                    datetime.fromisoformat(row.payload["old_due_at"]).replace(tzinfo=UTC)
                    if row.payload.get("old_due_at")
                    else None,
                    settings,
                ),
                "to": _local(
                    datetime.fromisoformat(row.payload["new_due_at"]).replace(tzinfo=UTC)
                    if row.payload.get("new_due_at")
                    else None,
                    settings,
                ),
            }
        if row.type == EventType.new_announcement:
            entry["url"] = row.payload.get("html_url")
        changes.append(entry)

    # The same new announcement can arrive as both an event and a direct announcement
    # row; the event wins (it carries the payload), the listing skips the duplicate.
    seen_announcements = {
        row.entity_id for row in rows if row.type == EventType.new_announcement
    }
    fresh = (
        await session.scalars(
            select(Announcement)
            .where(
                Announcement.course_id.in_(by_id),
                Announcement.posted_at.is_not(None),
                Announcement.posted_at >= start,
            )
            .order_by(Announcement.posted_at.desc())
            .limit(15)
        )
    ).all()
    for row in fresh:
        if row.canvas_id in seen_announcements:
            continue
        course = by_id[row.course_id]
        changes.append(
            {
                "type": "new_announcement",
                "what": row.title,
                "course": course.short_code or course.code,
                "when": _local(row.posted_at, settings),
                "url": row.html_url,
            }
        )

    return {
        "since": _local(start, settings),
        "today": datetime.now(settings.tz).date().isoformat(),
        "changes": changes,
    }


async def search_assignments(
    session: AsyncSession, settings: Settings, query: str, course_code: str | None = None
) -> dict:
    """Keyword search over assignment names, for "when is the robotics project due?".

    Returned whether or not the deadline has passed: someone asking by name wants that
    specific work, and "it was due last week" is a real answer.
    """
    courses = await _tracked_courses(session)
    target = _match_course(courses, course_code)
    if course_code and target is None:
        return {"error": f"No tracked course matching {course_code!r}."}

    scope = [target] if target else courses
    collapsed = await _live_assignments(session, settings, scope)

    needle = query.strip().lower()
    if not needle:
        return {"query": query, "matches": []}

    matches = [
        _assignment_row(assignment, course, settings)
        for assignment, course, _ in collapsed
        if needle in (assignment.name or "").lower()
    ]
    matches.sort(key=lambda r: r["due_at"] or "9999")
    return {"query": query, "matches": matches[:10]}


async def workload_forecast(
    session: AsyncSession, settings: Settings, week_offset: int = 0, weeks: int = 3
) -> dict:
    """Points-weighted load per week, for questions about capacity."""
    courses = await _tracked_courses(session)
    pairs = await _live_assignments(session, settings, courses)

    today = datetime.now(settings.tz).date()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)

    buckets: list[dict] = []
    for index in range(max(1, weeks)):
        week_start = start_of_week + timedelta(weeks=index)
        week_end = week_start + timedelta(days=7)
        items = []
        points = 0.0
        for assignment, course, _ in pairs:
            if assignment.due_at is None:
                continue
            due_local = assignment.due_at.astimezone(settings.tz).date()
            if week_start <= due_local < week_end:
                items.append(
                    {
                        "course": course.short_code or course.code,
                        "title": assignment.name,
                        "due_at": _local(assignment.due_at, settings),
                        "points_possible": assignment.points_possible,
                        "submitted": assignment.has_submitted,
                    }
                )
                points += assignment.points_possible or 0
        items.sort(key=lambda r: r["due_at"] or "")
        buckets.append(
            {
                "week_starting": week_start.isoformat(),
                "item_count": len(items),
                "total_points": round(points, 2),
                "items": items,
            }
        )

    return {"today": today.isoformat(), "weeks": buckets}


async def get_exams(
    session: AsyncSession, settings: Settings, within_days: int | None = None
) -> dict:
    """Exams and tests extracted from the syllabi, with countdowns."""
    courses = {c.id: c for c in await _tracked_courses(session)}
    if not courses:
        return {"exams": [], "note": "No courses are being tracked."}

    rows = (
        await session.scalars(select(Exam).where(Exam.course_id.in_(courses)).order_by(Exam.date))
    ).all()

    today = datetime.now(settings.tz).date()
    out: list[dict] = []
    for row in rows:
        days = (row.date - today).days if row.date else None
        if within_days is not None and (days is None or days < 0 or days > within_days):
            continue
        course = courses[row.course_id]
        out.append(
            {
                "course": course.short_code or course.code,
                "title": row.title,
                "kind": row.kind.value if row.kind else None,
                "date": row.date.isoformat() if row.date else None,
                "days_away": days,
                "start_time": row.start_time.strftime("%H:%M") if row.start_time else None,
                "location": row.location,
                "weight_pct": float(row.weight_pct) if row.weight_pct is not None else None,
                # Surfaced so the model can hedge appropriately on anything unverified,
                # rather than stating an unconfirmed extraction as fact.
                "confirmed": row.confirmed_by_user,
                "source_quote": row.source_quote,
            }
        )

    unconfirmed = sum(1 for e in out if not e["confirmed"])
    return {
        "today": today.isoformat(),
        "exams": out,
        "note": (
            None
            if not out
            else (
                f"{unconfirmed} of these are unconfirmed extractions from the syllabus - "
                "say so if you mention them."
                if unconfirmed
                else None
            )
        ),
    }


async def search_syllabus(
    session: AsyncSession, settings: Settings, query: str, course_code: str | None = None
) -> dict:
    """Keyword search over the text of syllabus documents.

    For questions about policy -- late submissions, missed tests, academic integrity --
    that live in the syllabus rather than in any structured Canvas field.
    """
    courses = await _tracked_courses(session)
    target = _match_course(courses, course_code)
    if course_code and target is None:
        return {"error": f"No tracked course matching {course_code!r}."}

    by_id = {c.id: c for c in ([target] if target else courses)}
    statement = select(File).where(File.course_id.in_(by_id), File.extracted_text.is_not(None))
    if query.strip():
        statement = statement.where(File.extracted_text.ilike(f"%{query.strip()}%"))

    rows = (await session.scalars(statement.limit(6))).all()

    matches: list[dict] = []
    needle = query.strip().lower()
    for row in rows:
        text = row.extracted_text or ""
        # Return the surrounding window rather than the whole document: a syllabus is
        # tens of thousands of characters and the answer is always local to the hit.
        excerpts: list[str] = []
        low = text.lower()
        start = 0
        while len(excerpts) < 3 and needle:
            hit = low.find(needle, start)
            if hit == -1:
                break
            excerpts.append(" ".join(text[max(0, hit - 250) : hit + 350].split()))
            start = hit + len(needle)
        matches.append(
            {
                "course": by_id[row.course_id].short_code or by_id[row.course_id].code,
                "document": row.filename,
                "excerpts": excerpts or [" ".join(text[:400].split())],
            }
        )

    return {"query": query, "matches": matches}


async def add_manual_item(
    session: AsyncSession,
    settings: Settings,
    title: str,
    course_code: str | None = None,
    due_at: str | None = None,
    kind: str = "task",
    notes: str | None = None,
) -> dict:
    """Record something Canvas has no idea about.

    Study sessions, group meetings, a reading the professor mentioned out loud. These
    live alongside Canvas data everywhere it matters -- the digest, the forecast, the
    calendar export.
    """
    courses = await _tracked_courses(session)
    course = _match_course(courses, course_code)

    when: datetime | None = None
    if due_at:
        try:
            parsed = datetime.fromisoformat(due_at.strip())
        except ValueError:
            return {
                "error": f"Could not read {due_at!r} as a date. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM."
            }
        # A bare date means the end of that day, which is what a person means by "due
        # friday" -- not midnight at its start.
        if parsed.hour == 0 and parsed.minute == 0 and len(due_at.strip()) <= 10:
            parsed = parsed.replace(hour=23, minute=59)
        when = parsed.replace(tzinfo=settings.tz).astimezone(UTC)

    item = ManualItem(
        course_id=course.id if course else None,
        title=title.strip(),
        due_at=when,
        kind=kind,
        notes=notes,
    )
    session.add(item)
    await session.flush()

    label = f"{course.short_code or course.code}: " if course else ""
    stamp = f" - due {when.astimezone(settings.tz):%a %d %b %H:%M}" if when else " (no date)"
    return {"added": f"{label}{item.title}{stamp}", "id": item.id}


async def list_contacts(
    session: AsyncSession, settings: Settings, course_code: str | None = None
) -> dict:
    """Known email addresses for course staff.

    Unconfirmed entries were pulled out of a syllabus automatically and may be wrong, so
    they are flagged -- emailing the wrong professor is worse than not having an address.
    """
    courses = await _tracked_courses(session)
    target = _match_course(courses, course_code)
    if course_code and target is None:
        return {"error": f"No tracked course matching {course_code!r}."}

    by_id = {c.id: c for c in ([target] if target else courses)}
    rows = (await session.scalars(select(Contact).where(Contact.course_id.in_(by_id)))).all()
    return {
        "contacts": [
            {
                "course": by_id[r.course_id].short_code or by_id[r.course_id].code
                if r.course_id
                else None,
                "name": r.name,
                "email": r.email,
                "role": r.role,
                "confirmed": r.confirmed_by_user,
                "found_in": r.source_quote,
            }
            for r in rows
        ]
    }


# --------------------------------------------------------------------------- registry

_COURSE_PARAM = {
    "type": "string",
    "description": "Course code such as MGAB03. Omit for all tracked courses.",
}

TOOLS: list[Tool] = [
    Tool(
        name="list_upcoming",
        description=(
            "Assignments due within the next N days, across all courses or one. Also "
            "returns published work that has no due date set, which is real graded work "
            "and easy to miss."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days ahead to look. 7 for 'this week'.",
                },
                "course_code": _COURSE_PARAM,
            },
            "required": ["days"],
            "additionalProperties": False,
        },
        fn=list_upcoming,
    ),
    Tool(
        name="get_course",
        description=(
            "Details of one course: full name, your sections, and its total assessment load."
        ),
        parameters={
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Course code, e.g. MGAB03."}},
            "required": ["code"],
            "additionalProperties": False,
        },
        fn=get_course,
    ),
    Tool(
        name="search_announcements",
        description=(
            "Keyword search over course announcements. Use this for questions about what "
            "an instructor said or posted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to look for."},
                "course_code": _COURSE_PARAM,
                "since": {"type": "string", "description": "Only after this date, YYYY-MM-DD."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        fn=search_announcements,
    ),
    Tool(
        name="get_grades",
        description=(
            "Scores received so far and the running total. Returns nothing if "
            "nothing has been graded."
        ),
        parameters={
            "type": "object",
            "properties": {"course_code": _COURSE_PARAM},
            "required": [],
            "additionalProperties": False,
        },
        fn=get_grades,
    ),
    Tool(
        name="get_exams",
        description=(
            "Exams, midterms and term tests taken from the course syllabi, with how many "
            "days away each is. Use for any question about exams."
        ),
        parameters={
            "type": "object",
            "properties": {
                "within_days": {
                    "type": "integer",
                    "description": "Only exams within this many days. Omit for all of them.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        fn=get_exams,
    ),
    Tool(
        name="search_syllabus",
        description=(
            "Search the text of syllabus documents. Use for policy questions - late "
            "submissions, missed tests, grading schemes, academic integrity."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to look for."},
                "course_code": _COURSE_PARAM,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        fn=search_syllabus,
    ),
    Tool(
        name="add_manual_item",
        description=(
            "Record something Canvas does not know about - a study session, a group "
            "meeting, a reading mentioned in class. Use when the user says to remember, "
            "add, or track something."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What it is."},
                "course_code": _COURSE_PARAM,
                "due_at": {
                    "type": "string",
                    "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM in the user's timezone.",
                },
                "kind": {"type": "string", "description": "task, meeting, reading, or exam."},
                "notes": {"type": "string", "description": "Anything else worth keeping."},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        fn=add_manual_item,
    ),
    Tool(
        name="list_contacts",
        description=(
            "Email addresses for instructors and TAs. Use before drafting an email so "
            "you know who to write to."
        ),
        parameters={
            "type": "object",
            "properties": {"course_code": _COURSE_PARAM},
            "required": [],
            "additionalProperties": False,
        },
        fn=list_contacts,
    ),
    Tool(
        name="list_overdue",
        description=(
            "Unsubmitted work whose due date has already passed. Use for any question "
            "about overdue, missed or late assignments."
        ),
        parameters={
            "type": "object",
            "properties": {"course_code": _COURSE_PARAM},
            "required": [],
            "additionalProperties": False,
        },
        fn=list_overdue,
    ),
    Tool(
        name="get_changes_since",
        description=(
            "What changed since a moment: new and removed assignments, moved deadlines, "
            "and new announcements. Use for questions like 'what's new' or 'what "
            "changed today'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "ISO date or datetime, e.g. 2026-09-30. Defaults to yesterday.",
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        fn=get_changes_since,
    ),
    Tool(
        name="search_assignments",
        description=(
            "Find assignments by name, whether upcoming or past due. Use when the user "
            "names a specific piece of work, e.g. 'when is the robotics project due?'"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words from the assignment title."},
                "course_code": _COURSE_PARAM,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        fn=search_assignments,
    ),
    Tool(
        name="workload_forecast",
        description=(
            "Assignments and total points per week, for questions about how busy a "
            "stretch of time is or whether there is capacity to take something on."
        ),
        parameters={
            "type": "object",
            "properties": {
                "week_offset": {
                    "type": "integer",
                    "description": "0 for the current week, 1 for next week, and so on.",
                },
                "weeks": {"type": "integer", "description": "How many weeks to report. Default 3."},
            },
            "required": [],
            "additionalProperties": False,
        },
        fn=workload_forecast,
    ),
]

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_schemas() -> list[dict[str, Any]]:
    return [tool.schema() for tool in TOOLS]


async def count_tracked_courses(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Course).where(Course.is_tracked, Course.is_active)
        )
        or 0
    )


__all__ = [
    "TOOLS",
    "TOOLS_BY_NAME",
    "Tool",
    "count_tracked_courses",
    "date",
    "tool_schemas",
]
