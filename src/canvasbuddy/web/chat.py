"""The web chat: deterministic answers first, the LLM agent for everything else.

Two properties matter more than cleverness here.

**Grounding.** Every deterministic answer is assembled from rows the tools returned --
course, title, due date, link -- so the text can only contain facts that exist in the
synced Canvas data. When a question names work the database has never seen, the honest
answer is "I couldn't find it", not a guess; the search-before-LLM path below exists
mostly to make that rule enforceable.

**Cost.** "What's due tomorrow?" needs zero model calls: the answer is a filtered
database query rendered in a fixed shape. Only questions the pattern layer cannot
recognise reach the LLM agent, which has the same tool set and the same rule: never
invent a deadline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.agent import memory
from canvasbuddy.agent.loop import run_agent
from canvasbuddy.agent.prompt import build_system_prompt
from canvasbuddy.agent.tools import TOOLS_BY_NAME, _tracked_courses
from canvasbuddy.config import Settings
from canvasbuddy.digest import render
from canvasbuddy.llm.openrouter import LLMError, OpenRouterClient
from canvasbuddy.models import Course

log = logging.getLogger(__name__)

CHANNEL = "web"

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE
)

_GREETING_RE = re.compile(
    r"^(hi+h?|hey+|hello|yo|sup|good (morning|afternoon|evening)|thanks|thank you)"
    r"[!. ,]*(studybuddy|postbot|buddy)?[!. ,]*$",
    re.IGNORECASE,
)
_HELP_RE = re.compile(
    r"^\s*(?:help\b[?.!]*|what can you do\??|what can i ask( you)?\??|who are you\??"
    r"|what do you do\??)\s*$",
    re.IGNORECASE,
)
_OVERDUE_RE = re.compile(
    r"\boverdue\b|\bmissed\b|past due|past its due|\blate work\b", re.IGNORECASE
)
_UPDATES_RE = re.compile(
    r"what('| i)?s new|whats changed|what.*(changed|happened)|any(new|thing new| updates?|"
    r" announcements?| posts?| news)|new (announcements?|posts?|updates?)|updates? since"
    r"|since (yesterday|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|did (any|my|a) ?(teacher|instructor|prof|professor|course)s? post",
    re.IGNORECASE,
)
_ANNOUNCEMENT_ONLY_RE = re.compile(r"announcements?|posts?\b|posted", re.IGNORECASE)
_NEXT_RE = re.compile(
    r"\bnext\b[^.]*\b(assignment|deadline|thing|due|quiz|test|exam|task|up)\b"
    r"|\b(assignment|deadline|quiz|test|exam)\b[^.]*\bnext\b",
    re.IGNORECASE,
)
_DUE_RE = re.compile(
    r"\bdue\b|\bdeadline|\bwhat should i work on\b|\bam i busy\b|\bto ?night\b", re.IGNORECASE
)
_NAMED_ITEM_RE = re.compile(
    r"when (?:is|was|are|'s)\s+(?P<item>.+?)\s*(?:due|happening)\??\s*$", re.IGNORECASE
)
_QUIZ_WORD_RE = re.compile(r"\bquiz\b|\btest\b|\bmidterm\b|\bexam\b", re.IGNORECASE)


@dataclass(frozen=True)
class Source:
    """One Canvas row an answer was built from, shown to the user as a link."""

    course: str
    title: str
    due_at: str | None = None
    url: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"course": self.course, "title": self.title, "dueAt": self.due_at, "url": self.url}


@dataclass
class ChatAnswer:
    text: str
    kind: str
    sources: list[Source] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.text,
            "kind": self.kind,
            "sources": [s.as_dict() for s in self.sources],
        }


# ------------------------------------------------------------------ formatting


def _parse_local(dt_text: str | None) -> datetime | None:
    if not dt_text:
        return None
    try:
        dt = datetime.fromisoformat(dt_text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fmt_due(dt_text: str | None, settings: Settings) -> str:
    """A tool-provided ISO string rendered the way a person says it."""
    dt = _parse_local(dt_text)
    if dt is None:
        return dt_text or ""
    day = render.format_day(dt, settings.tz)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{day}, {hour}:{dt.strftime('%M')} {dt.strftime('%p').lower()}"


def _row_source(row: dict[str, Any]) -> Source:
    return Source(
        course=row.get("course") or "",
        title=row.get("title") or row.get("what") or "",
        due_at=row.get("due_at"),
        url=row.get("url"),
    )


def _due_lines(rows: list[dict[str, Any]], settings: Settings) -> list[str]:
    lines = []
    for row in rows:
        bits = []
        when = _fmt_due(row.get("due_at"), settings)
        if when:
            bits.append(f"due {when}")
        if row.get("points_possible"):
            points = float(row["points_possible"])
            bits.append(f"{points:g} pts" if points != 1 else "1 pt")
        if row.get("your_section"):
            bits.append(str(row["your_section"]))
        suffix = f" ({' · '.join(bits)})" if bits else ""
        lines.append(f"{row.get('course')} — {row.get('title')}{suffix}")
    return lines


def _fmt_date(dt_text: str | None, settings: Settings) -> str:
    dt = _parse_local(dt_text)
    return render.format_day(dt, settings.tz) if dt else dt_text or ""


def _list_text(
    noun_singular: str, noun_plural: str, rows: list[dict[str, Any]], settings: Settings
) -> str:
    if not rows:
        return ""
    header = (
        f"Just one {noun_singular}:" if len(rows) == 1 else f"You have {len(rows)} {noun_plural}:"
    )
    return f"{header}\n{_numbered(_due_lines(rows, settings))}"


def _numbered(lines: list[str]) -> str:
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))


# ------------------------------------------------------------- course matching


def match_course_mentions(text: str, courses: list[Course]) -> list[tuple[Course, str]]:
    """Courses named in the message, with the text that matched, longest match first.

    People say "AUSTA", "austa 153", or "my stats class"; Canvas says "AUSTA 153H3".
    Two directions of matching cover that: a candidate appearing in the message
    ("austa 153" inside "austa 153 quiz"), and a message word being a code prefix
    ("comp" starting "comp 214"). A bare prefix only counts when the candidate
    continues with a separator, so "the" does not match "THEA 101".
    """
    lowered = text.lower()
    if not lowered.strip():
        return []
    matches: dict[int, tuple[Course, str]] = {}
    for course in courses:
        for candidate in _course_names(course):
            hit = ""
            if candidate in lowered:
                hit = candidate
            else:
                for token in re.findall(r"[a-z0-9]{3,}", lowered):
                    rest = candidate[len(token):]
                    if candidate.startswith(token) and rest and not rest[0].isalnum():
                        hit = token
                        break
            if hit:
                current = matches.get(course.id)
                if current is None or len(hit) > len(current[1]):
                    matches[course.id] = (course, hit)
    ranked = sorted(matches.values(), key=lambda pair: -len(pair[1]))
    # Keep only courses matched by the longest text: "COMP" matching two courses is
    # ambiguity to ask about, but "austa 153" matching just AUSTA 153 is decided.
    if ranked:
        longest = len(ranked[0][1])
        ranked = [pair for pair in ranked if len(pair[1]) == longest]
    return ranked


def _course_names(course: Course) -> list[str]:
    values = [course.short_code, course.code, course.nickname, course.title]
    return [v.strip().lower() for v in values if v and v.strip()]


# -------------------------------------------------------------- moment parsing


def _local_today(settings: Settings, now: datetime) -> datetime:
    return now.astimezone(settings.tz).replace(hour=0, minute=0, second=0, microsecond=0)


def _since_moment(text: str, settings: Settings, now: datetime) -> tuple[datetime, str]:
    """The start of the window a "what's new" question asks about, plus a label."""
    today = _local_today(settings, now)
    lowered = text.lower()
    if "yesterday" in lowered:
        return today - timedelta(days=1), "since yesterday"
    if "this week" in lowered:
        return today - timedelta(days=today.weekday()), "this week"
    if "today" in lowered:
        return today, "since this morning"
    match = _WEEKDAY_RE.search(lowered)
    if match and "since" in lowered:
        target = _WEEKDAYS[match.group(1)]
        delta = (today.weekday() - target) % 7 or 7
        return today - timedelta(days=delta), f"since {match.group(1)}"
    return now - timedelta(hours=24), "in the last day"


def _due_window(text: str, settings: Settings, now: datetime) -> tuple[Any, Any, str]:
    """(first local date, last local date, label) for a due question."""
    today = _local_today(settings, now).date()
    lowered = text.lower()

    if re.search(r"\b(to ?night|today)\b", lowered):
        return today, today, "today"
    if "tomorrow" in lowered:
        tomorrow = today + timedelta(days=1)
        return tomorrow, tomorrow, "tomorrow"
    if "next week" in lowered:
        monday = today + timedelta(days=7 - today.weekday())
        return monday, monday + timedelta(days=6), "next week"
    if "this week" in lowered:
        return today, today + timedelta(days=6), "this week"
    match = _WEEKDAY_RE.search(lowered)
    if match:
        target = _WEEKDAYS[match.group(1)]
        delta = (target - today.weekday()) % 7
        if delta == 0 and "next" in lowered:
            delta = 7
        target_date = today + timedelta(days=delta)
        return today, target_date, match.group(1)
    return today, today + timedelta(days=6), "this week"


# ------------------------------------------------------------------- dispatch


async def answer_message(
    session: AsyncSession,
    settings: Settings,
    llm: OpenRouterClient | None,
    text: str,
) -> ChatAnswer:
    """Answer one dashboard question. Deterministic first; the agent gets the rest."""
    text = (text or "").strip()
    now = datetime.now(UTC)

    if not text:
        return ChatAnswer(_help_text(), kind="help")

    lowered = text.lower()
    if _GREETING_RE.match(text):
        return ChatAnswer(
            "Hey! Ask me about assignments, deadlines, announcements, or what changed "
            "in your courses.",
            kind="greeting",
        )
    if _HELP_RE.match(text):
        return ChatAnswer(_help_text(), kind="help")

    courses = await _tracked_courses(session)
    mentions = match_course_mentions(text, courses)
    course = mentions[0][0] if len(mentions) == 1 else None
    if len(mentions) > 1:
        return _disambiguate(mentions)

    # Overdue beats due-window: "anything overdue?" must not read as a generic due list.
    if _OVERDUE_RE.search(text):
        return await _answer_overdue(session, settings, course)

    if _UPDATES_RE.search(text):
        return await _answer_changes(session, settings, course, text, now)

    if _NEXT_RE.search(text) and not course:
        return await _answer_next(session, settings, lowered, now)

    overview_re = re.compile(r"\bshow\b|\beverything\b|\bwhat('s| is) there\b", re.IGNORECASE)
    if course and overview_re.search(text):
        return await _answer_course_overview(session, settings, course, now)

    # "When is the robotics project due?" — search by name before the generic due
    # window, so a question about work that does not exist gets the honest not-found
    # answer instead of an empty week list.
    named = _NAMED_ITEM_RE.search(text)
    if named:
        return await _answer_named_item(session, settings, named.group("item"), course)

    if _DUE_RE.search(text):
        return await _answer_due(session, settings, course, text, now)

    return await _answer_with_agent(session, settings, llm, text)


def _help_text() -> str:
    return (
        "I keep an eye on your Canvas courses. Try:\n"
        "- What's due this week?\n"
        "- Anything due tomorrow?\n"
        "- What's overdue?\n"
        "- What's new since yesterday?\n"
        "- What's my next assignment?\n"
        "- Anything new in <course>?\n"
        "- When is <assignment> due?"
    )


def _disambiguate(mentions: list[tuple[Course, str]]) -> ChatAnswer:
    needle = mentions[0][1]
    codes = [c.short_code or c.code for c, _ in mentions]
    listing = "\n".join(codes)
    return ChatAnswer(
        f'I found {len(codes)} courses matching "{needle}":\n\n{listing}\n\n'
        "Which one did you mean?",
        kind="clarify",
    )


# --------------------------------------------------------------- answer bodies


async def _upcoming_rows(
    session: AsyncSession, settings: Settings, course: Course | None
) -> list[dict[str, Any]]:
    tool = TOOLS_BY_NAME["list_upcoming"]
    code = None if course is None else (course.short_code or course.code)
    result = await tool.fn(session, settings, days=31, course_code=code)
    if not isinstance(result, dict) or result.get("error"):
        return []
    return list(result.get("due") or [])


def _rows_between(
    rows: list[dict[str, Any]], settings: Settings, start: Any, end: Any
) -> list[dict[str, Any]]:
    """Filter tool rows to an inclusive local-date window, before any rendering."""
    out = []
    for row in rows:
        due = _parse_local(row.get("due_at"))
        if due is None:
            continue
        local_date = due.astimezone(settings.tz).date()
        if start <= local_date <= end:
            out.append(row)
    return out


async def _answer_due(
    session: AsyncSession,
    settings: Settings,
    course: Course | None,
    text: str,
    now: datetime,
) -> ChatAnswer:
    start, end, label = _due_window(text, settings, now)
    rows = await _upcoming_rows(session, settings, course)
    rows = _rows_between(rows, settings, start, end)

    if not rows:
        scope = f" in {course.short_code or course.code}" if course else ""
        return ChatAnswer(
            f"You're clear for now.\n\n"
            f"I couldn't find anything due {label}{scope} in your synced Canvas courses.",
            kind="clear",
        )

    body = _list_text(f"thing due {label}", f"things due {label}", rows, settings)
    return ChatAnswer(body, kind="due", sources=[_row_source(row) for row in rows])


async def _answer_overdue(
    session: AsyncSession, settings: Settings, course: Course | None
) -> ChatAnswer:
    tool = TOOLS_BY_NAME["list_overdue"]
    code = None if course is None else (course.short_code or course.code)
    result = await tool.fn(session, settings, course_code=code)
    if not isinstance(result, dict) or result.get("error"):
        return ChatAnswer(
            "I couldn't check overdue work right now — the Canvas data didn't come back.",
            kind="error",
        )
    rows = list(result.get("overdue") or [])
    if not rows:
        return ChatAnswer("Nothing is overdue. You're all caught up.", kind="clear")
    body = _list_text("thing overdue", "things overdue", rows, settings)
    return ChatAnswer(body, kind="overdue", sources=[_row_source(row) for row in rows])


async def _answer_next(
    session: AsyncSession, settings: Settings, lowered: str, now: datetime
) -> ChatAnswer:
    rows = await _upcoming_rows(session, settings, None)
    today = _local_today(settings, now).date()
    rows = _rows_between(rows, settings, today, today + timedelta(days=31))
    if _QUIZ_WORD_RE.search(lowered):
        quiz_rows = [
            row
            for row in rows
            if _QUIZ_WORD_RE.search(row.get("title") or "")
        ]
        rows = quiz_rows
    if not rows:
        noun = "quiz or test coming up" if _QUIZ_WORD_RE.search(lowered) else "assignment due"
        return ChatAnswer(
            f"I couldn't find any {noun} in your synced Canvas courses.", kind="clear"
        )
    row = rows[0]
    when = _fmt_due(row.get("due_at"), settings)
    return ChatAnswer(
        f"Next up: {row.get('course')} — {row.get('title')}, due {when}.",
        kind="next",
        sources=[_row_source(row)],
    )


async def _answer_changes(
    session: AsyncSession,
    settings: Settings,
    course: Course | None,
    text: str,
    now: datetime,
) -> ChatAnswer:
    since, label = _since_moment(text, settings, now)
    tool = TOOLS_BY_NAME["get_changes_since"]
    result = await tool.fn(session, settings, since=since.isoformat())
    if not isinstance(result, dict) or result.get("error"):
        return ChatAnswer("I couldn't load your recent updates just now.", kind="error")

    announcement_only = bool(_ANNOUNCEMENT_ONLY_RE.search(text))
    changes = list(result.get("changes") or [])
    if course:
        code = course.short_code or course.code
        changes = [c for c in changes if c.get("course") == code]

    if announcement_only:
        changes = [c for c in changes if c.get("type") == "new_announcement"]

    if not changes:
        subject = f" for {course.short_code or course.code}" if course else ""
        return ChatAnswer(
            f"No updates {label}{subject}. Everything in your synced courses is as it was.",
            kind="clear",
        )

    lines = []
    sources = []
    for change in changes[:8]:
        course_code = change.get("course") or "Canvas"
        what = change.get("what") or "something"
        kind_text = {
            "new_assignment": "new assignment",
            "assignment_removed": "removed",
            "new_announcement": "new announcement",
            "due_date_changed": "deadline moved",
        }.get(change.get("type"), change.get("type"))
        detail = ""
        moved = change.get("changed") or {}
        if moved:
            detail = (
                f" (was {_fmt_due(moved.get('from'), settings) or 'no date'}, "
                f"now {_fmt_due(moved.get('to'), settings) or 'no date'})"
            )
        lines.append(f"{course_code} — {kind_text}: {what}{detail}")
        sources.append(
            Source(
                course=course_code,
                title=what,
                due_at=moved.get("to") if moved else None,
                url=change.get("url"),
            )
        )

    body = f"{len(changes)} update{'s' if len(changes) != 1 else ''} {label}:\n\n{_numbered(lines)}"
    if len(changes) > 8:
        body += f"\n\n(+{len(changes) - 8} more — ask about a specific course)"
    return ChatAnswer(body, kind="changes", sources=sources)


async def _answer_course_overview(
    session: AsyncSession, settings: Settings, course: Course, now: datetime
) -> ChatAnswer:
    code = course.short_code or course.code
    rows = await _upcoming_rows(session, settings, course)
    today = _local_today(settings, now).date()
    rows = _rows_between(rows, settings, today, today + timedelta(days=31))
    tool = TOOLS_BY_NAME["search_announcements"]
    ann = await tool.fn(session, settings, query="", course_code=code)
    announcements = list(ann.get("matches") or [])[:3] if isinstance(ann, dict) else []

    parts = []
    sources: list[Source] = []
    if rows:
        parts.append(f"Coming up in {code}:\n{_numbered(_due_lines(rows[:5], settings))}")
        sources.extend(_row_source(row) for row in rows[:5])
    else:
        parts.append(f"No upcoming work in {code}.")
    if announcements:
        ann_lines = [
            f"— {a.get('title')} ({_fmt_date(a.get('posted_at'), settings)})"
            for a in announcements
        ]
        parts.append("Latest announcements:\n" + "\n".join(ann_lines))
        sources.extend(
            Source(course=code, title=a.get("title") or "", url=a.get("url"))
            for a in announcements
        )
    return ChatAnswer("\n\n".join(parts), kind="course", sources=sources)


async def _answer_named_item(
    session: AsyncSession, settings: Settings, item: str, course: Course | None
) -> ChatAnswer:
    item = re.sub(r"^(the|a|an|my)\s+", "", item.strip(), flags=re.IGNORECASE)
    tool = TOOLS_BY_NAME["search_assignments"]
    code = None if course is None else (course.short_code or course.code)
    result = await tool.fn(session, settings, query=item, course_code=code)
    if not isinstance(result, dict) or result.get("error"):
        return ChatAnswer(
            f"I couldn't find anything matching \u201c{item}\u201d in your Canvas data.",
            kind="not_found",
        )
    rows = list(result.get("matches") or [])
    if not rows:
        return ChatAnswer(
            f"I couldn't find a \u201c{item}\u201d in your Canvas data. If it was posted "
            "recently, try a sync first.",
            kind="not_found",
        )
    return ChatAnswer(
        _list_text(
            f"match for \u201c{item}\u201d", f"matches for \u201c{item}\u201d", rows, settings
        ),
        kind="due",
        sources=[_row_source(row) for row in rows],
    )


# ------------------------------------------------------------------ agent path


async def _answer_with_agent(
    session: AsyncSession,
    settings: Settings,
    llm: OpenRouterClient | None,
    text: str,
) -> ChatAnswer:
    if llm is None:
        return ChatAnswer(
            "I can answer due dates, announcements and course changes on my own, but "
            "that question needs the AI side of me, which isn't configured. Set "
            "OPENROUTER_API_KEY (or another supported provider) to unlock it.",
            kind="ai_unavailable",
        )

    history = await memory.load_history(session, settings, CHANNEL)
    system = await build_system_prompt(session, settings, channel=CHANNEL)
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": text}]

    try:
        reply = await run_agent(session, settings, llm, messages)
    except LLMError as exc:
        log.warning("chat agent failed: %s", exc)
        return ChatAnswer(
            "I couldn't reach the AI provider for that one. Simple due-date and update "
            "questions still work without it.",
            kind="ai_error",
        )

    await memory.save_turn(session, CHANNEL, "user", text)
    await memory.save_turn(session, CHANNEL, "assistant", reply.text)

    return ChatAnswer(
        reply.text or "I didn't get an answer for that — try rephrasing?",
        kind="agent",
        sources=_sources_from_messages(messages),
    )


def _sources_from_messages(messages: list[dict[str, Any]]) -> list[Source]:
    """Pull linkable rows back out of the tool traffic the agent generated.

    The agent's prose is grounded by its tool calls, so the rows those calls returned
    are exactly the evidence behind the answer. Plucking them out of the transcript
    keeps the source links honest without asking the model to also emit JSON.
    """
    sources: list[Source] = []
    seen: set[tuple[str, str]] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        rows: list[dict[str, Any]] = []
        for key in ("due", "overdue", "matches", "changes", "graded"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(r for r in value if isinstance(r, dict))
        for row in rows:
            source = _row_source(row)
            if not source.title:
                continue
            key = (source.course, source.title)
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)
        if len(sources) >= 8:
            break
    return sources[:8]
