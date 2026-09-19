"""Pulling assessments out of a syllabus with a model.

The whole design assumes the model will sometimes be wrong, and makes that survivable:

* Every assessment must carry a **verbatim ``source_quote``**, and the quote is checked
  against the source text. A row whose quote cannot be found is discarded -- that is the
  difference between a wrong answer you can audit and one that merely reads plausibly.
* Any date outside the course's own term window is rejected rather than stored.
* Anything below ``CONFIRM_THRESHOLD`` is stored unconfirmed and has to be approved before
  it counts as fact.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import LLMError, OpenRouterClient
from canvasbuddy.models import Course, Exam, ExamKind, ExamSource, File

log = logging.getLogger(__name__)

#: Below this an assessment is not trusted until the user confirms it.
CONFIRM_THRESHOLD = 0.8

#: Syllabi run long and the tail is usually policy boilerplate; the assessment table is
#: near the front. Trimming keeps the call cheap without losing what matters.
_MAX_CHARS = 40_000

_SYSTEM = """\
You extract assessment information from university course syllabi.

Return ONLY a JSON object, no prose, no code fences, with this shape:

{"assessments": [{
  "title": "Term Test 1",
  "kind": "midterm|final|term_test|quiz|presentation",
  "date": "2026-10-15 or null",
  "start_time": "19:00 or null",
  "duration_min": 90,
  "location": "IC 130 or null",
  "weight_pct": 25,
  "source_quote": "the exact sentence from the document that says this",
  "confidence": 0.95
}], "late_policy": "... or null", "notes": "... or null"}

Rules:
- source_quote must be copied VERBATIM from the document. Do not paraphrase, correct \
spelling, or join separate sentences. It is checked against the source and the entry is \
thrown away if it does not match.
- Use null for anything the document does not state. Never guess a date. A syllabus \
saying "Date TBD" means date is null, not an invented date.
- confidence reflects how clearly the document states it: 0.95 for an explicit date, \
0.5 for something you inferred from context.
- Only real assessments. Ignore readings, office hours, and textbook registration.
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_KINDS = {k.value for k in ExamKind}


@dataclass
class ExtractionResult:
    course_code: str
    created: list[Exam] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    late_policy: str | None = None

    def summary(self) -> str:
        lines = [f"{self.course_code}: {len(self.created)} assessment(s)"]
        lines += [
            f"    {e.title} — {e.date or 'no date'}"
            f"{f' · {e.weight_pct}%' if e.weight_pct else ''}"
            f"{'' if e.confirmed_by_user else '  (needs confirming)'}"
            for e in self.created
        ]
        lines += [f"    rejected: {title} — {why}" for title, why in self.rejected]
        return "\n".join(lines)


def _normalise(text: str) -> str:
    """Collapse whitespace so a quote check is not defeated by line wrapping."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _parse_payload(raw: str) -> dict:
    """Read the model's JSON, tolerating fences or a stray sentence around it."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(raw)
    if not match:
        raise LLMError("model did not return JSON")
    return json.loads(match.group(0))


def _coerce_date(value: object) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _coerce_time(value: object) -> time | None:
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(value.strip().upper().replace(" ", ""), fmt).time()
        except ValueError:
            continue
    return None


def _in_term(when: date | None, course: Course) -> bool:
    """A date outside the course's own term is a hallucination, not a deadline."""
    if when is None:
        return True
    if course.term_start_at and when < course.term_start_at.date():
        return False
    if course.term_end_at and when > course.term_end_at.date():
        return False
    return True


async def extract_assessments(
    session: AsyncSession,
    settings: Settings,
    llm: OpenRouterClient,
    course: Course,
    source_text: str,
    *,
    source_type: ExamSource,
    source_file: File | None = None,
) -> ExtractionResult:
    result = ExtractionResult(course_code=course.short_code or course.code)
    text = source_text[:_MAX_CHARS]

    choice = await llm.chat(
        [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Course: {course.short_code or course.code} — "
                    f"{course.nickname or course.title or course.name}\n"
                    f"Term runs "
                    f"{course.term_start_at.date() if course.term_start_at else 'unknown'} to "
                    f"{course.term_end_at.date() if course.term_end_at else 'unknown'}.\n\n"
                    f"--- SYLLABUS ---\n{text}"
                ),
            },
        ],
        tools=None,
        model=settings.extraction_model,
        max_tokens=4096,
    )

    payload = _parse_payload(choice.get("message", {}).get("content") or "")
    haystack = _normalise(source_text)

    existing = {
        (row.title or "").strip().lower()
        for row in (await session.scalars(select(Exam).where(Exam.course_id == course.id))).all()
    }

    for entry in payload.get("assessments") or []:
        title = (entry.get("title") or "").strip()
        if not title:
            continue

        quote = (entry.get("source_quote") or "").strip()
        if not quote or _normalise(quote) not in haystack:
            # The single most valuable guard here: a quote the document does not contain
            # means the surrounding facts were invented too.
            result.rejected.append((title, "source quote not found in the document"))
            continue

        when = _coerce_date(entry.get("date"))
        if not _in_term(when, course):
            result.rejected.append((title, f"date {when} falls outside the term"))
            continue

        if title.lower() in existing:
            continue

        kind = str(entry.get("kind") or "").strip().lower()
        confidence = entry.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        exam = Exam(
            course_id=course.id,
            title=title,
            kind=ExamKind(kind) if kind in _KINDS else ExamKind.quiz,
            date=when,
            start_time=_coerce_time(entry.get("start_time")),
            duration_min=entry.get("duration_min")
            if isinstance(entry.get("duration_min"), int)
            else None,
            location=(entry.get("location") or None),
            weight_pct=entry.get("weight_pct")
            if isinstance(entry.get("weight_pct"), int | float)
            else None,
            source_type=source_type,
            source_quote=quote,
            confidence=confidence,
            # High-confidence rows are usable straight away; the rest wait for a human.
            confirmed_by_user=bool(confidence is not None and confidence >= CONFIRM_THRESHOLD),
        )
        session.add(exam)
        result.created.append(exam)
        existing.add(title.lower())

    result.late_policy = payload.get("late_policy")
    if source_file is not None and result.created:
        log.info("Extracted %d assessment(s) from %s", len(result.created), source_file.filename)
    return result
