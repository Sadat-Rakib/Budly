"""Pydantic models for the Canvas payloads we consume.

Only the fields CanvasBuddy actually uses are modelled, with ``extra="ignore"`` so
Canvas adding fields never breaks a sync. The value is the opposite direction: if
Canvas *renames* or *removes* something we depend on, validation fails loudly at the
boundary instead of quietly producing a ``None`` three layers down.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def html_to_text(html: str | None) -> str | None:
    """Crude HTML flattening, good enough for previews and search.

    Deliberately dependency-free: announcement bodies are short, and pulling in a
    parser for this would not change the result.
    """
    if not html:
        return None
    text = _TAG_RE.sub(" ", html)
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ):
        text = text.replace(entity, char)
    return _WS_RE.sub(" ", text).strip() or None


class CanvasModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Term(CanvasModel):
    id: int | None = None
    name: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class Course(CanvasModel):
    id: int
    name: str
    course_code: str | None = None
    syllabus_body: str | None = None
    term: Term | None = None

    @property
    def display_code(self) -> str:
        """A short label for the digest.

        Canvas course codes at UofT look like "MGAB03H3 F LEC01 20269"; the first token
        is the part a human recognises.
        """
        raw = self.course_code or self.name
        return raw.split()[0] if raw.split() else raw


class Submission(CanvasModel):
    workflow_state: str | None = None
    submitted_at: datetime | None = None
    score: float | None = None
    grade: str | None = None
    graded_at: datetime | None = None


class Assignment(CanvasModel):
    id: int
    name: str
    description: str | None = None
    due_at: datetime | None = None
    unlock_at: datetime | None = None
    lock_at: datetime | None = None
    points_possible: float | None = None
    submission_types: list[str] = Field(default_factory=list)
    html_url: str | None = None
    workflow_state: str | None = None
    submission: Submission | None = None

    @property
    def has_submitted(self) -> bool:
        state = (self.submission.workflow_state if self.submission else None) or ""
        return state not in {"", "unsubmitted"}


class Announcement(CanvasModel):
    id: int
    title: str
    message: str | None = None
    posted_at: datetime | None = None
    html_url: str | None = None
    context_code: str | None = None
    user_name: str | None = None

    @property
    def course_canvas_id(self) -> int | None:
        """Announcements identify their course as ``course_455965``."""
        if not self.context_code or not self.context_code.startswith("course_"):
            return None
        try:
            return int(self.context_code.removeprefix("course_"))
        except ValueError:
            return None

    @property
    def body_text(self) -> str | None:
        return html_to_text(self.message)


class Enrollment(CanvasModel):
    id: int
    type: str | None = None
    course_section_id: int | None = None
    enrollment_state: str | None = None


class Section(CanvasModel):
    id: int
    name: str


class PlannerItem(CanvasModel):
    """One row of the user's planner feed.

    ``plannable_id`` is the id of the underlying object -- the assignment id for an
    assignment -- which is what lets planner rows be joined onto stored assignments.
    """

    plannable_id: int | None = None
    plannable_type: str | None = None
    plannable_date: datetime | None = None
    context_name: str | None = None
    submissions: dict | bool | None = None

    @field_validator("submissions", mode="before")
    @classmethod
    def _normalize_submissions(cls, v: object) -> object:
        # Canvas sends `false` rather than an object for non-assignment items.
        return v if isinstance(v, dict | bool) else None

    @property
    def is_submitted(self) -> bool:
        return bool(isinstance(self.submissions, dict) and self.submissions.get("submitted"))
