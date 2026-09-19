"""iCalendar export.

Hand-rolled rather than pulled from a library: RFC 5545 for a feed of all-day and timed
events is a few dozen lines, and the awkward parts (line folding, escaping, CRLF) are
exactly the parts a dependency would hide until something renders wrong in Apple
Calendar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.config import Settings
from canvasbuddy.digest.builder import collapse_section_variants
from canvasbuddy.models import Assignment, Course, Exam, ManualItem

_PRODID = "-//CanvasBuddy//Canvas//EN"


def _escape(text: str) -> str:
    """RFC 5545 §3.3.11: backslash, semicolon, comma and newline are special."""
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold at 75 octets, continuation lines beginning with a space.

    Unfolded long lines are the single most common reason a calendar file is rejected.
    """
    encoded = line.encode()
    if len(encoded) <= 75:
        return line
    chunks = [encoded[:75]]
    rest = encoded[75:]
    while rest:
        chunks.append(rest[:74])
        rest = rest[74:]
    return "\r\n ".join(c.decode(errors="ignore") for c in chunks)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _event(
    uid: str,
    summary: str,
    *,
    start: datetime | date,
    end: datetime | date | None = None,
    description: str | None = None,
    location: str | None = None,
) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{_stamp(datetime.now(UTC))}"]
    if isinstance(start, datetime):
        lines.append(f"DTSTART:{_stamp(start)}")
        lines.append(f"DTEND:{_stamp(end if isinstance(end, datetime) else start)}")
    else:
        # All-day events are exclusive of DTEND, so a one-day event ends the next day.
        lines.append(f"DTSTART;VALUE=DATE:{start:%Y%m%d}")
        finish = end if isinstance(end, date) else start + timedelta(days=1)
        lines.append(f"DTEND;VALUE=DATE:{finish:%Y%m%d}")
    lines.append(f"SUMMARY:{_escape(summary)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    lines.append("END:VEVENT")
    return lines


async def build_ics(session: AsyncSession, settings: Settings) -> str:
    """Every deadline, exam and manual item as an importable calendar."""
    courses = {
        c.id: c
        for c in (
            await session.scalars(select(Course).where(Course.is_tracked, Course.is_active))
        ).all()
    }

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:CanvasBuddy",
    ]

    if courses:
        assignments = (
            await session.scalars(
                select(Assignment).where(
                    Assignment.course_id.in_(courses),
                    ~Assignment.is_deleted,
                    Assignment.due_at.is_not(None),
                )
            )
        ).all()

        # Collapse per-section duplicates before writing, exactly as the digest and the
        # tools do. Without this, MGAB03's four copies of one Group Project become four
        # calendar entries on the same evening.
        pairs = [
            (row, courses[row.course_id]) for row in assignments if not row.is_gradebook_column
        ]
        for row, course, label in collapse_section_variants(pairs):
            summary = f"{course.short_code or course.code}: {row.name}"
            lines += _event(
                f"assignment-{row.canvas_id}@canvasbuddy",
                summary,
                # A deadline is a moment, not a meeting, so it gets a zero-length event
                # at the due time rather than an arbitrary hour-long block.
                start=row.due_at,
                end=row.due_at,
                description=row.html_url,
                location=label,
            )

        exams = (
            await session.scalars(
                select(Exam).where(Exam.course_id.in_(courses), Exam.date.is_not(None))
            )
        ).all()
        for row in exams:
            course = courses[row.course_id]
            title = f"{course.short_code or course.code}: {row.title}"
            if not row.confirmed_by_user:
                title += " (unconfirmed)"
            if row.start_time:
                begin = datetime.combine(row.date, row.start_time, tzinfo=settings.tz)
                finish = begin + timedelta(minutes=row.duration_min or 120)
                lines += _event(
                    f"exam-{row.id}@canvasbuddy",
                    title,
                    start=begin,
                    end=finish,
                    location=row.location,
                    description=row.source_quote,
                )
            else:
                lines += _event(
                    f"exam-{row.id}@canvasbuddy",
                    title,
                    start=row.date,
                    location=row.location,
                    description=row.source_quote,
                )

    manual = (await session.scalars(select(ManualItem).where(~ManualItem.done))).all()
    for row in manual:
        if row.due_at is None:
            continue
        course = courses.get(row.course_id or -1)
        prefix = f"{course.short_code or course.code}: " if course else ""
        lines += _event(
            f"manual-{row.id}@canvasbuddy",
            f"{prefix}{row.title}",
            start=row.due_at,
            end=row.due_at,
            description=row.notes,
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
