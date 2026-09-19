"""Finding and ingesting a course's documents.

Discovery goes through **modules**, not the files endpoint. Measured against the real
account, ``/courses/:id/files`` returns 403 for three courses in four -- instructors
routinely hide the Files tab -- while ``/modules?include[]=items`` works everywhere and
the file items inside resolve to downloadable files regardless.

Downloads are gated on ``content_hash`` so an unchanged document is never re-read and
never re-sent to a model.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.canvas.client import CanvasClient, CanvasError
from canvasbuddy.documents.extract import ExtractionError, content_score, extract_text
from canvasbuddy.models import Course, File

log = logging.getLogger(__name__)

#: Documents larger than this are skipped. Syllabi are small; anything bigger is a slide
#: deck or a video, and downloading it wastes bandwidth and Canvas quota.
_MAX_BYTES = 12 * 1024 * 1024


@dataclass
class IngestReport:
    downloaded: int = 0
    skipped_unchanged: int = 0
    skipped_large: int = 0
    unsupported: int = 0
    failed: int = 0
    locked: list[tuple[str, str]] = field(default_factory=list)
    candidates: list[tuple[str, str, int]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"downloaded:  {self.downloaded}",
            f"unchanged:   {self.skipped_unchanged}",
            f"too large:   {self.skipped_large}",
            f"unsupported: {self.unsupported}",
            f"failed:      {self.failed}",
        ]
        if self.locked:
            lines.append("locked by the instructor (can't be read):")
            lines += [f"  {course:<10} {name[:52]}" for course, name in self.locked]
        if self.candidates:
            lines.append("syllabus candidates:")
            lines += [
                f"  {course:<10} score {score:>3}  {name[:52]}"
                for course, name, score in sorted(self.candidates, key=lambda c: -c[2])
            ]
        return "\n".join(lines)


def _module_file_items(modules: list[dict]) -> list[dict]:
    return [
        item
        for module in modules
        for item in (module.get("items") or [])
        if item.get("type") == "File" and item.get("url")
    ]


async def ingest_course_documents(
    session: AsyncSession,
    client: CanvasClient,
    course: Course,
    report: IngestReport,
    *,
    force: bool = False,
) -> None:
    """Download and extract every readable document in one course."""
    try:
        modules = await client.get_modules(course.canvas_id)
    except CanvasError as exc:
        log.warning("Cannot read modules for %s: %s", course.code, exc)
        return

    items = _module_file_items(modules)
    if not items:
        log.info("%s has no file items in its modules", course.code)
        return

    existing = {
        row.canvas_id: row
        for row in (await session.scalars(select(File).where(File.course_id == course.id))).all()
    }

    for item in items:
        try:
            meta = await client.get_file(item["url"])
        except CanvasError as exc:
            log.info("Could not resolve %s: %s", item.get("title"), exc)
            report.failed += 1
            continue

        canvas_id = meta.get("id")
        if canvas_id is None:
            continue

        size = meta.get("size") or 0
        if size > _MAX_BYTES:
            report.skipped_large += 1
            continue

        row = existing.get(canvas_id)
        download_url = meta.get("url")
        if not download_url:
            # Canvas withholds the download url for a locked file. This happens to live
            # documents mid-term -- MGHB02's syllabus was readable one hour and locked
            # the next -- so it is reported rather than silently skipped, otherwise the
            # course just appears to have no syllabus for no visible reason.
            if meta.get("locked_for_user") or meta.get("hidden_for_user"):
                report.locked.append(
                    (course.short_code or course.code, meta.get("display_name") or str(canvas_id))
                )
            continue

        try:
            data = await client.download(download_url)
        except CanvasError as exc:
            log.info("Download failed for %s: %s", meta.get("display_name"), exc)
            report.failed += 1
            continue

        digest = hashlib.sha256(data).hexdigest()
        if row is not None and row.content_hash == digest and not force:
            report.skipped_unchanged += 1
            continue

        if row is None:
            row = File(canvas_id=canvas_id, course_id=course.id)
            session.add(row)

        row.filename = meta.get("display_name") or meta.get("filename") or str(canvas_id)
        row.content_type = meta.get("content-type")
        row.size = size
        row.url = meta.get("html_url") or download_url
        row.content_hash = digest
        row.downloaded_at = datetime.now(UTC)
        report.downloaded += 1

        try:
            _, text = extract_text(data)
        except ExtractionError:
            # Slide decks, images and spreadsheets all land here. Normal, not a failure.
            row.extracted_text = None
            report.unsupported += 1
            continue
        except Exception:  # noqa: BLE001 - a malformed document must not stop the pass
            log.exception("Extraction crashed on %s", row.filename)
            row.extracted_text = None
            report.failed += 1
            continue

        row.extracted_text = text
        score = content_score(text)
        if score:
            report.candidates.append((course.short_code or course.code, row.filename, score))


async def best_syllabus(session: AsyncSession, course: Course) -> File | None:
    """The document in a course most likely to be its syllabus.

    Chosen by content score, never by filename. MGAB03 contains a file named
    "MGAB03 L04 - Kong - Fall 2026.pdf" that is a textbook registration card and scores
    zero; a name-based heuristic would send it to the model and get invented exam dates
    back with high confidence.
    """
    from canvasbuddy.documents.extract import SYLLABUS_SCORE_FLOOR

    rows = (
        await session.scalars(
            select(File).where(File.course_id == course.id, File.extracted_text.is_not(None))
        )
    ).all()

    best: File | None = None
    best_score = 0
    for row in rows:
        score = content_score(row.extracted_text or "")
        if score > best_score:
            best, best_score = row, score

    return best if best_score >= SYLLABUS_SCORE_FLOOR else None
