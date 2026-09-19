"""The diff engine: pure functions from (stored, incoming) to events.

Deliberately free of I/O and of the ORM, so every branch below is directly testable
without a database. The sync worker loads rows, calls these, and persists the result.

Two behaviours are worth stating explicitly because getting them wrong is what makes a
notification bot feel broken:

**Bootstrapping.** The first time a course is seen, its entire backlog would otherwise
be reported as new. Prior-art bots document exactly this failure -- the first run posts
a whole semester of history. Events are therefore suppressed until a course has been
seen once.

**Change detection, not just presence.** Deduplicating on id alone means a due date
that moves is never re-reported. Every field that matters is compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from canvasbuddy.models import EventType


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise to timezone-aware UTC.

    Postgres hands back aware datetimes and Canvas sends ``Z``-suffixed strings, but a
    naive value from either side would compare unequal to an identical aware one and
    produce a phantom "changed" event on every single sync pass.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class AssignmentState:
    """The subset of an assignment that the diff engine compares."""

    canvas_id: int
    name: str
    due_at: datetime | None = None
    points_possible: float | None = None
    workflow_state: str | None = None
    score: float | None = None
    has_submitted: bool = False

    def normalized(self) -> AssignmentState:
        return AssignmentState(
            canvas_id=self.canvas_id,
            name=self.name,
            due_at=_as_utc(self.due_at),
            points_possible=self.points_possible,
            workflow_state=self.workflow_state,
            score=self.score,
            has_submitted=self.has_submitted,
        )


@dataclass(frozen=True)
class PendingEvent:
    """An event not yet written to the database."""

    type: EventType
    entity_type: str
    canvas_id: int
    payload: dict[str, Any] = field(default_factory=dict)


def diff_assignment(
    stored: AssignmentState | None,
    incoming: AssignmentState,
    *,
    bootstrapped: bool,
) -> list[PendingEvent]:
    """Compare one assignment against its stored version.

    Preconditions:
        bootstrapped is False only on a course's very first sync
    """
    incoming = incoming.normalized()

    if stored is None:
        if not bootstrapped:
            return []
        return [
            PendingEvent(
                type=EventType.new_assignment,
                entity_type="assignment",
                canvas_id=incoming.canvas_id,
                payload={
                    "name": incoming.name,
                    "due_at": _iso(incoming.due_at),
                    "points_possible": incoming.points_possible,
                },
            )
        ]

    stored = stored.normalized()
    events: list[PendingEvent] = []

    if stored.due_at != incoming.due_at:
        events.append(
            PendingEvent(
                type=EventType.due_date_changed,
                entity_type="assignment",
                canvas_id=incoming.canvas_id,
                payload={
                    "name": incoming.name,
                    "old_due_at": _iso(stored.due_at),
                    "new_due_at": _iso(incoming.due_at),
                },
            )
        )

    if stored.points_possible != incoming.points_possible:
        events.append(
            PendingEvent(
                type=EventType.points_changed,
                entity_type="assignment",
                canvas_id=incoming.canvas_id,
                payload={
                    "name": incoming.name,
                    "old_points": stored.points_possible,
                    "new_points": incoming.points_possible,
                },
            )
        )

    if stored.workflow_state != incoming.workflow_state:
        events.append(
            PendingEvent(
                type=EventType.state_changed,
                entity_type="assignment",
                canvas_id=incoming.canvas_id,
                payload={
                    "name": incoming.name,
                    "old_state": stored.workflow_state,
                    "new_state": incoming.workflow_state,
                },
            )
        )

    # Only a score arriving is interesting. A score being revised is also reported, but
    # a score vanishing (an instructor un-posting grades) is not worth a notification.
    if incoming.score is not None and stored.score != incoming.score:
        events.append(
            PendingEvent(
                type=EventType.grade_posted,
                entity_type="assignment",
                canvas_id=incoming.canvas_id,
                payload={
                    "name": incoming.name,
                    "score": incoming.score,
                    "points_possible": incoming.points_possible,
                    "previous_score": stored.score,
                },
            )
        )

    return events


def detect_removals(
    stored: dict[int, AssignmentState],
    incoming_ids: set[int],
    *,
    bootstrapped: bool,
) -> list[PendingEvent]:
    """Assignments that were present last pass and are absent now.

    Canvas deletes and unpublishes silently -- there is no tombstone -- so disappearance
    has to be treated as an event in its own right.
    """
    if not bootstrapped:
        return []
    return [
        PendingEvent(
            type=EventType.assignment_removed,
            entity_type="assignment",
            canvas_id=canvas_id,
            payload={"name": state.name, "due_at": _iso(_as_utc(state.due_at))},
        )
        for canvas_id, state in stored.items()
        if canvas_id not in incoming_ids
    ]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
