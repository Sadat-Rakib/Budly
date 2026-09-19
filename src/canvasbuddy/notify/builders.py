"""Deterministic content builders for nudge/review/checkin + Slack digest renderer."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.agent.tools import _live_assignments
from canvasbuddy.config import Settings
from canvasbuddy.digest.builder import DigestContent
from canvasbuddy.models import Announcement, Assignment, Course, Event, EventType, Exam
from canvasbuddy.notify.content import NotificationContent, Section

_QUIZ_RE = re.compile(r"\b(quiz|test|midterm|exam|final)\b", re.IGNORECASE)
_NON_SUBMITTABLE = {"none", "not_graded", "on_paper"}
_MAX_PER_SECTION = 15


def _fmt_due(dt: datetime | None, tz) -> str:
    if dt is None:
        return "no date"
    local = dt.astimezone(tz)
    # "Wed 11:59 PM" — no zero-pad, Windows-safe
    hour = local.strftime("%I").lstrip("0") or "12"
    return f"{local.strftime('%a')} {hour}:{local.strftime('%M')} {local.strftime('%p')}"


def _fmt_day(dt: datetime | None, tz) -> str:
    if dt is None:
        return "no date"
    local = dt.astimezone(tz)
    return f"{local.strftime('%a')} {local.strftime('%b')} {local.day}"


def _pts(a: Assignment) -> str:
    if a.points_possible:
        v = float(a.points_possible)
        return f"{v:g} pts" if v != 1 else "1 pt"
    return "ungraded"


def _is_missed(a: Assignment, now: datetime) -> bool:
    if a.due_at is None or a.due_at >= now:
        return False
    if a.has_submitted:
        return False
    if a.score is not None:
        return False
    if (a.workflow_state or "published") != "published":
        # Only published counts; unpublished/others are not actionable.
        if a.workflow_state not in (None, "published"):
            return False
    types = set(a.submission_types or [])
    if types and types <= _NON_SUBMITTABLE:
        return False
    if a.is_gradebook_column:
        return False
    return True


def _is_quiz_like(a: Assignment) -> bool:
    if "online_quiz" in (a.submission_types or []):
        return True
    return bool(_QUIZ_RE.search(a.name or ""))


async def _courses_map(session: AsyncSession) -> dict[int, Course]:
    rows = (await session.scalars(select(Course).where(Course.is_tracked, Course.is_active))).all()
    return {c.id: c for c in rows}


def _cap(lines: list[str]) -> list[str]:
    if len(lines) > _MAX_PER_SECTION:
        return lines[:_MAX_PER_SECTION] + [f"+{len(lines) - _MAX_PER_SECTION} more — /week"]
    return lines


async def build_nudge_content(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> NotificationContent | None:
    """Unsubmitted items due within 24h. None means send nothing."""
    now = now or datetime.now(UTC)
    courses = await _courses_map(session)
    if not courses:
        return None
    collapsed = await _live_assignments(session, settings, list(courses.values()))
    horizon = now + timedelta(hours=24)
    pending = [
        (a, c)
        for a, c, _ in collapsed
        if a.due_at is not None
        and now <= a.due_at <= horizon
        and not a.has_submitted
        and a.score is None
        and not a.is_gradebook_column
    ]
    if not pending:
        return None
    pending.sort(key=lambda t: t[0].due_at)
    if len(pending) == 1:
        a, c = pending[0]
        code = c.short_code or c.code
        title = f"Due tomorrow: {code} — {a.name} ({_fmt_due(a.due_at, settings.tz)})"
        return NotificationContent(
            kind="nudge", title=title, sections=[], footer="/week for details"
        )
    lines = [
        f"{(c.short_code or c.code)} — {a.name} ({_fmt_due(a.due_at, settings.tz)})"
        for a, c in pending
    ]
    return NotificationContent(
        kind="nudge",
        title="Due in the next 24h",
        sections=[Section(heading="Due soon", lines=_cap(lines))],
        footer="/week for details",
    )


async def build_review_content(
    session: AsyncSession, settings: Settings, now: datetime | None = None
) -> NotificationContent:
    now = now or datetime.now(UTC)
    tz = settings.tz
    lookback = timedelta(days=settings.review_lookback_days)
    lookahead = timedelta(days=settings.review_lookahead_days)
    courses = await _courses_map(session)
    collapsed = (
        await _live_assignments(session, settings, list(courses.values())) if courses else []
    )

    # --- 1. Still to submit ---
    missed_open: list[tuple[Assignment, Course]] = []
    missed_closed: list[tuple[Assignment, Course]] = []
    for a, c, _ in collapsed:
        if not _is_missed(a, now):
            continue
        if a.lock_at is not None and a.lock_at <= now:
            missed_closed.append((a, c))
        else:
            missed_open.append((a, c))
    missed_open.sort(key=lambda t: t[0].due_at or now)
    missed_closed.sort(key=lambda t: t[0].due_at or now)

    still_lines: list[str] = []
    # Group by course for readability
    for group, items in (("open", missed_open), ("closed", missed_closed)):
        by_course: dict[str, list[tuple[Assignment, Course]]] = defaultdict(list)
        for a, c in items:
            by_course[c.label].append((a, c))
        for label, pairs in by_course.items():
            still_lines.append(f"{label}")
            for a, _c in pairs:
                quiz_mark = " 📝" if _is_quiz_like(a) else ""
                state = (
                    "closed"
                    if group == "closed"
                    else (f"open, locks {_fmt_due(a.lock_at, tz)}" if a.lock_at else "open")
                )
                still_lines.append(
                    f"• {a.name}{quiz_mark} — was due {_fmt_due(a.due_at, tz)}"
                    f" · {_pts(a)} · {state}"
                )
    still_lines = _cap(still_lines)

    # --- 2. Last 7 days stats ---
    window_start = now - lookback
    submitted_on_time = late = missed = 0
    graded_new: list[str] = []
    for a, _c, _ in collapsed:
        # submitted counts
        if a.submitted_at and window_start <= a.submitted_at <= now:
            if a.due_at and a.submitted_at > a.due_at:
                late += 1
            else:
                submitted_on_time += 1
        if a.due_at and window_start <= a.due_at < now and not a.has_submitted and a.score is None:
            if _is_missed(a, now):
                missed += 1
        if (
            settings.show_grades_in_digest
            and a.graded_at
            and window_start <= a.graded_at <= now
            and a.score is not None
        ):
            total = (
                f"{a.score:g}/{float(a.points_possible):g}" if a.points_possible else f"{a.score:g}"
            )
            graded_new.append(f"{a.name}: {total}")
    stats_line = f"{submitted_on_time} on time · {late} late · {missed} missed"
    last7_lines = [f"Last 7 days: {stats_line}"]
    if graded_new:
        last7_lines += _cap(graded_new)

    # --- 3. Tests & quizzes ahead (14d) ---
    quiz_horizon = now + timedelta(days=14)
    quiz_lines: list[str] = []
    for a, c, _ in collapsed:
        if a.due_at is None or not (now <= a.due_at <= quiz_horizon):
            continue
        if a.has_submitted:
            continue
        if _is_quiz_like(a):
            days = (a.due_at.astimezone(tz).date() - now.astimezone(tz).date()).days
            code = c.short_code or c.code
            quiz_lines.append(f"{_fmt_day(a.due_at, tz)} — {code} {a.name} (in {days} days)")
    # extracted exams
    if courses:
        exams = (
            await session.scalars(
                select(Exam)
                .where(Exam.course_id.in_(courses), Exam.date.is_not(None))
                .order_by(Exam.date)
            )
        ).all()
        today = now.astimezone(tz).date()
        for e in exams:
            if e.date is None:
                continue
            delta = (e.date - today).days
            if 0 <= delta <= 14:
                c = courses[e.course_id]
                code = c.short_code or c.code
                quiz_lines.append(
                    f"{e.date.strftime('%a %b')} {e.date.day} — {code} {e.title} (in {delta} days)"
                )
    quiz_lines = sorted(set(quiz_lines))[:_MAX_PER_SECTION]

    # --- 4. Next 7 days ---
    next_end = now + lookahead
    upcoming: list[tuple[Assignment, Course]] = [
        (a, c)
        for a, c, _ in collapsed
        if a.due_at
        and now <= a.due_at <= next_end
        and not a.has_submitted
        and not a.is_gradebook_column
    ]
    upcoming.sort(key=lambda t: t[0].due_at)
    by_day: dict[str, list[tuple[Assignment, Course]]] = defaultdict(list)
    total_pts = 0.0
    for a, c in upcoming:
        by_day[_fmt_day(a.due_at, tz)].append((a, c))
        total_pts += float(a.points_possible or 0)
    next_lines: list[str] = []
    heaviest = ""
    heaviest_n = 0
    for day, pairs in by_day.items():
        if len(pairs) > heaviest_n:
            heaviest_n = len(pairs)
            heaviest = day
        for a, c in pairs:
            code = c.short_code or c.code
            next_lines.append(f"{day}: {code} {a.name} ({_pts(a)})")
    # delta vs last 7 days points
    last_pts = sum(
        float(a.points_possible or 0)
        for a, _c, _ in collapsed
        if a.due_at and window_start <= a.due_at < now
    )
    delta = total_pts - last_pts
    footer_next = (
        f"{len(upcoming)} items · {total_pts:g} pts ({delta:+g} vs last week)"
        f" · heaviest: {heaviest or '—'}"
    )
    if next_lines:
        next_lines = _cap(next_lines)
        next_lines.append(footer_next)
    else:
        next_lines = ["Nothing due in the next 7 days.", footer_next]

    # --- 5. Changes ---
    change_lines: list[str] = []
    # announcements last 7d
    anns = (
        (
            await session.scalars(
                select(Announcement)
                .where(
                    Announcement.course_id.in_(courses) if courses else True,
                    Announcement.posted_at.is_not(None),
                    Announcement.posted_at >= window_start,
                )
                .order_by(Announcement.posted_at.desc())
                .limit(15)
            )
        ).all()
        if courses
        else []
    )
    for an in anns:
        c = courses[an.course_id]
        change_lines.append(f"{c.short_code or c.code} announced: {an.title}")
    # due-date changes from events
    evts = (
        await session.scalars(
            select(Event)
            .where(
                Event.type.in_([EventType.due_date_changed, EventType.new_assignment]),
                Event.created_at >= window_start,
            )
            .order_by(Event.created_at.desc())
            .limit(15)
        )
    ).all()
    for e in evts:
        payload = e.payload or {}
        name = payload.get("name") or payload.get("title") or f"{e.entity_type} {e.entity_id}"
        if e.type == EventType.due_date_changed:
            change_lines.append(f"{name} due date moved")
        else:
            change_lines.append(f"New: {name}")
    change_lines = _cap(change_lines)

    title = f"📅 Saturday review — {now.astimezone(tz).strftime('%b')} {now.astimezone(tz).day}"
    sections = [
        Section(heading="⚠️ Still to submit", lines=still_lines or ["Nothing missed."]),
        Section(heading="✅ Last 7 days", lines=last7_lines),
        Section(
            heading="📝 Tests & quizzes ahead", lines=quiz_lines or ["None in the next 14 days."]
        ),
        Section(heading="🗓 Next 7 days", lines=next_lines),
        Section(heading="📣 Changes", lines=change_lines or ["No changes."]),
    ]
    return NotificationContent(
        kind="review", title=title, sections=sections, footer="/week for details · /mute to pause"
    )


async def build_checkin_content(
    session: AsyncSession,
    settings: Settings,
    now: datetime | None = None,
    *,
    since: datetime | None = None,
) -> NotificationContent:
    now = now or datetime.now(UTC)
    tz = settings.tz
    since = since or (now - timedelta(hours=7))  # 08:00 -> 15:00 same day
    courses = await _courses_map(session)
    collapsed = (
        await _live_assignments(session, settings, list(courses.values())) if courses else []
    )

    urgent: list[str] = []
    horizon = now + timedelta(hours=72)
    for a, c, _ in collapsed:
        if a.has_submitted or a.score is not None or a.is_gradebook_column:
            continue
        overdue_open = (
            a.due_at is not None and a.due_at < now and (a.lock_at is None or a.lock_at > now)
        )
        due_soon = a.due_at is not None and now <= a.due_at <= horizon
        if overdue_open or due_soon:
            code = c.short_code or c.code
            urgent.append(f"{code} — {a.name} ({_fmt_due(a.due_at, tz)})")
    urgent = _cap(sorted(set(urgent)))

    fresh: list[str] = []
    if courses:
        anns = (
            await session.scalars(
                select(Announcement)
                .where(
                    Announcement.course_id.in_(courses),
                    Announcement.posted_at.is_not(None),
                    Announcement.posted_at >= since,
                )
                .order_by(Announcement.posted_at.desc())
                .limit(10)
            )
        ).all()
        for an in anns:
            c = courses[an.course_id]
            fresh.append(f"{c.short_code or c.code} announced: {an.title}")
        evts = (
            await session.scalars(
                select(Event)
                .where(Event.created_at >= since)
                .order_by(Event.created_at.desc())
                .limit(10)
            )
        ).all()
        for e in evts:
            payload = e.payload or {}
            name = payload.get("name") or payload.get("title") or f"{e.entity_type} {e.entity_id}"
            fresh.append(f"{e.type.value}: {name}")
    fresh = _cap(fresh)

    title = "Saturday check-in — what's still open"
    if not urgent and not fresh:
        return NotificationContent(
            kind="checkin",
            title=title,
            sections=[
                Section(
                    heading="Status", lines=["Nothing urgent. Enjoy the rest of your Saturday."]
                )
            ],
            footer="/week for details",
        )
    sections = []
    if urgent:
        sections.append(Section(heading="Still open", lines=urgent))
    if fresh:
        sections.append(Section(heading="New since this morning", lines=fresh))
    return NotificationContent(
        kind="checkin", title=title, sections=sections, footer="/week for details"
    )


def render_digest_slack(content: DigestContent, settings: Settings) -> str:
    """Render DigestContent to Slack mrkdwn (never by parsing MarkdownV2)."""
    from canvasbuddy.notify.notifiers import escape_slack

    tz = settings.tz
    local_date = content.local_date.astimezone(tz)
    title = f"📅 {local_date.strftime('%A, %b')} {local_date.day}"
    parts = [f"*{escape_slack(title)}*"]

    def block(emoji: str, heading: str, items, with_due: bool) -> None:
        if not items:
            return
        # group by course_label
        order: list[str] = []
        groups: dict[str, list] = {}
        for it in items:
            key = it.course_label or it.course_code
            groups.setdefault(key, []).append(it)
            if key not in order:
                order.append(key)
        lines = [f"*{escape_slack(f'{emoji} {heading}')}*"]
        for key in order:
            lines.append(f"*{escape_slack(key)}*")
            for it in groups[key]:
                bits: list[str] = []
                if with_due and it.due_at:
                    local = it.due_at.astimezone(tz)
                    hour = local.strftime("%I").lstrip("0") or "12"
                    bits.append(
                        f"{local.strftime('%a')} {hour}:"
                        f"{local.strftime('%M')}{local.strftime('%p')}"
                    )
                if it.detail:
                    bits.append(it.detail)
                suffix = f" — {' · '.join(bits)}" if bits else ""
                lines.append(f"• {escape_slack(it.title)}{escape_slack(suffix)}")
        parts.append("\n".join(lines))

    block("⚠️", "DUE TODAY", content.due_today, True)
    block("🔜", "NEXT 72 HOURS", content.upcoming, True)
    block("🎯", "EXAM COUNTDOWN", content.exams, False)
    block("📢", "NEW SINCE YESTERDAY", content.announcements, False)
    block("📊", "GRADED", content.graded, False)
    block("🗓", "NO DUE DATE SET", content.undated, False)
    if content.is_empty:
        parts.append("Nothing due, nothing new. Enjoy it.")
    return "\n\n".join(parts)
