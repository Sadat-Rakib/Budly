"""Ingest documents, then extract assessments from the best one per course.

Kept separate from both halves so the CLI, the sync worker and the bot all drive the same
sequence rather than each assembling their own.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.agent.extraction import ExtractionResult, extract_assessments
from canvasbuddy.canvas.client import CanvasClient
from canvasbuddy.canvas.schemas import html_to_text
from canvasbuddy.config import Settings
from canvasbuddy.documents.extract import content_score, looks_like_syllabus
from canvasbuddy.documents.fetch import IngestReport, best_syllabus, ingest_course_documents
from canvasbuddy.llm.openrouter import LLMError, OpenRouterClient
from canvasbuddy.models import Course, ExamSource

log = logging.getLogger(__name__)


async def run_extraction(
    session: AsyncSession,
    settings: Settings,
    client: CanvasClient,
    llm: OpenRouterClient | None,
    *,
    course_code: str | None = None,
    force: bool = False,
) -> tuple[IngestReport, list[ExtractionResult]]:
    courses = list(
        (await session.scalars(select(Course).where(Course.is_tracked, Course.is_active))).all()
    )
    if course_code:
        needle = course_code.strip().lower()
        courses = [
            c for c in courses if needle in {(c.short_code or "").lower(), (c.code or "").lower()}
        ]

    report = IngestReport()
    results: list[ExtractionResult] = []

    for course in courses:
        await ingest_course_documents(session, client, course, report, force=force)
        await session.flush()

        source_text: str | None = None
        source_type = ExamSource.syllabus_pdf
        source_file = await best_syllabus(session, course)

        if source_file is not None:
            source_text = source_file.extracted_text
        elif course.syllabus_html:
            # PHLB18 publishes its syllabus as Canvas HTML rather than a file, so this is
            # a real path and not just a fallback.
            candidate = html_to_text(course.syllabus_html) or ""
            if looks_like_syllabus(candidate):
                source_text, source_type = candidate, ExamSource.syllabus_html

        if not source_text:
            log.info("%s: nothing that reads like a syllabus", course.short_code or course.code)
            continue

        if llm is None:
            log.warning("OPENROUTER_API_KEY not set; skipping extraction")
            continue

        log.info(
            "%s: extracting from %s (score %d)",
            course.short_code or course.code,
            source_file.filename if source_file else "syllabus_body",
            content_score(source_text),
        )
        try:
            results.append(
                await extract_assessments(
                    session,
                    settings,
                    llm,
                    course,
                    source_text,
                    source_type=source_type,
                    source_file=source_file,
                )
            )
        except LLMError as exc:
            log.warning("Extraction failed for %s: %s", course.short_code or course.code, exc)

    return report, results
