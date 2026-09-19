"""SQLAlchemy 2.0 models.

Timezone rule: every timestamp is stored UTC (``TIMESTAMPTZ``) and rendered in the
user's zone only at the presentation layer.
"""

from __future__ import annotations

import enum
from datetime import date as date_t
from datetime import datetime
from datetime import time as time_t

from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_id: Mapped[int] = mapped_column(unique=True, index=True)
    #: The Canvas course code, e.g. "MGAB03H3".
    code: Mapped[str] = mapped_column(String(128))
    #: The code as a person says it, e.g. "MGAB03".
    short_code: Mapped[str | None] = mapped_column(String(32))
    #: The raw Canvas name, header and all.
    name: Mapped[str] = mapped_column(Text)
    #: The name with the canvasbuddy's header stripped, e.g. "Introductory Management
    #: Accounting".
    title: Mapped[str | None] = mapped_column(Text)
    #: A name you chose yourself, e.g. "Managerial Accounting". Wins over `title`
    #: everywhere it is set, and survives every sync.
    nickname: Mapped[str | None] = mapped_column(Text)
    term_name: Mapped[str | None] = mapped_column(String(128))
    term_id: Mapped[int | None]
    term_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    term_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    syllabus_html: Mapped[str | None] = mapped_column(Text)
    syllabus_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    instructor_name: Mapped[str | None] = mapped_column(String(256))
    color_hex: Mapped[str | None] = mapped_column(String(7))

    #: Sections this user is actually enrolled in, e.g. MGAB03H3-F-LEC04-20269.
    #: A user can hold several enrolments in one course, so this is a list.
    enrolled_sections: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Term filter. Non-term shells (orientation modules, residence) stay stored
    #: but are excluded from sync and digest.
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Null until the first sync completes. While null, diffing stays silent so a
    #: newly added course does not flood the digest with its entire backlog.
    bootstrapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    assignments: Mapped[list[Assignment]] = relationship(back_populates="course")
    announcements: Mapped[list[Announcement]] = relationship(back_populates="course")

    @property
    def label(self) -> str:
        """What the digest prints: short code plus a readable name.

        Falls back through nickname, then parsed title, then whatever Canvas gave us,
        so a course always renders as *something* even if the name is unparseable.
        """
        name = self.nickname or self.title
        code = self.short_code or self.code
        return f"{code} · {name}" if name and name != code else code


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_id: Mapped[int] = mapped_column(unique=True, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)

    name: Mapped[str] = mapped_column(Text)
    description_html: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    unlock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lock_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    points_possible: Mapped[float | None]
    submission_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    html_url: Mapped[str | None] = mapped_column(Text)

    has_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score: Mapped[float | None]
    grade: Mapped[str | None] = mapped_column(String(32))
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workflow_state: Mapped[str | None] = mapped_column(String(32))

    #: True when this assignment appeared in /planner/items. Planner resolves section
    #: overrides server-side but silently drops anything with a null due date, so it
    #: is an overlay on the canonical /assignments list, not a replacement for it.
    on_planner: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Section suffix parsed from the name, e.g. L04 from "Group Project - L04".
    #: Some instructors create one assignment per section instead of using overrides,
    #: and Canvas exposes no API signal for which one is yours.
    section_hint: Mapped[str | None] = mapped_column(String(32))

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    course: Mapped[Course] = relationship(back_populates="assignments")

    @property
    def is_gradebook_column(self) -> bool:
        """A gradebook placeholder, not real work: no due date and nothing to submit.

        Canvas models these as assignments (e.g. "Midterm Score"), but they must never
        appear in a what-is-due list.
        """
        return self.due_at is None and (self.submission_types or []) == ["none"]


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_id: Mapped[int] = mapped_column(unique=True, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)

    title: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    author_name: Mapped[str | None] = mapped_column(String(256))
    html_url: Mapped[str | None] = mapped_column(Text)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    course: Mapped[Course] = relationship(back_populates="announcements")


class File(Base):
    """Syllabus PDFs and slide decks.

    Populated at P2; the table exists now so the schema does not churn later.
    """

    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_id: Mapped[int] = mapped_column(unique=True, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)

    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None]
    url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExamKind(enum.StrEnum):
    midterm = "midterm"
    final = "final"
    term_test = "term_test"
    quiz = "quiz"
    presentation = "presentation"


class ExamSource(enum.StrEnum):
    syllabus_html = "syllabus_html"
    syllabus_pdf = "syllabus_pdf"
    announcement = "announcement"
    manual = "manual"


class Exam(Base):
    """LLM-extracted, human-confirmed. Populated at P2."""

    __tablename__ = "exams"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)

    title: Mapped[str] = mapped_column(Text)
    kind: Mapped[ExamKind] = mapped_column(Enum(ExamKind, name="exam_kind"))
    date: Mapped[date_t | None] = mapped_column(Date)
    start_time: Mapped[time_t | None] = mapped_column(Time)
    duration_min: Mapped[int | None]
    location: Mapped[str | None] = mapped_column(String(256))
    weight_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))

    source_type: Mapped[ExamSource] = mapped_column(Enum(ExamSource, name="exam_source"))
    #: The verbatim sentence this was extracted from, so a wrong answer is auditable.
    source_quote: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EventType(enum.StrEnum):
    new_assignment = "new_assignment"
    due_date_changed = "due_date_changed"
    points_changed = "points_changed"
    state_changed = "state_changed"
    new_announcement = "new_announcement"
    grade_posted = "grade_posted"
    assignment_removed = "assignment_removed"
    exam_detected = "exam_detected"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[EventType] = mapped_column(Enum(EventType, name="event_type"), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: Null means not yet included in a digest.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ChatMessage(Base):
    """Conversation memory for the P1 chat agent."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    tool_calls: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Contact(Base):
    """A person you might email: an instructor from a syllabus, or a TA you added.

    ``confirmed`` matters for the same reason it does on exams -- an address extracted
    from a document can be wrong, and emailing the wrong professor is worse than not
    having the address at all.
    """

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), default="syllabus")
    source_quote: Mapped[str | None] = mapped_column(Text)
    confirmed_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("course_id", "email", name="uq_contact_per_course"),)


class ManualItem(Base):
    """Something you told the bot about that Canvas does not know."""

    __tablename__ = "manual_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="task")
    notes: Mapped[str | None] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Setting(Base):
    """Small mutable runtime state -- mute windows, watermarks.

    Environment variables cannot be written at runtime, and a full table per flag is
    overkill for values a user toggles from chat.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Digest(Base):
    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Calendar date in the *user's* timezone, which is what "today's digest" means.
    local_date: Mapped[date_t] = mapped_column(Date)
    channel: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32), default="morning")

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    body_md: Mapped[str] = mapped_column(Text)
    items: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        # Makes a double send impossible at the database level rather than merely
        # unlikely: if two ticks race, one inserts and the other raises.
        UniqueConstraint("local_date", "channel", "kind", name="uq_digest_per_day"),
        Index("ix_digests_local_date", "local_date"),
    )
