"""Diff engine. Every branch, no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from canvasbuddy.models import EventType
from canvasbuddy.sync.diff import AssignmentState, detect_removals, diff_assignment

DUE = datetime(2026, 11, 16, 4, 59, 59, tzinfo=UTC)


def state(**overrides: object) -> AssignmentState:
    base: dict[str, object] = {
        "canvas_id": 1,
        "name": "Problem Set 2",
        "due_at": DUE,
        "points_possible": 100.0,
        "workflow_state": "published",
        "score": None,
        "has_submitted": False,
    }
    base.update(overrides)
    return AssignmentState(**base)  # type: ignore[arg-type]


def types(events: list) -> list[EventType]:
    return [e.type for e in events]


class TestBootstrap:
    def test_first_sight_of_a_course_is_silent(self) -> None:
        """The whole point: a new course must not dump its backlog into the digest."""
        assert diff_assignment(None, state(), bootstrapped=False) == []

    def test_new_assignment_after_bootstrap_is_reported(self) -> None:
        events = diff_assignment(None, state(), bootstrapped=True)
        assert types(events) == [EventType.new_assignment]
        assert events[0].payload["name"] == "Problem Set 2"


class TestChanges:
    def test_unchanged_emits_nothing(self) -> None:
        assert diff_assignment(state(), state(), bootstrapped=True) == []

    def test_moved_due_date_carries_both_values(self) -> None:
        moved = DUE + timedelta(days=3)
        events = diff_assignment(state(), state(due_at=moved), bootstrapped=True)
        assert types(events) == [EventType.due_date_changed]
        assert events[0].payload["old_due_at"] == DUE.isoformat()
        assert events[0].payload["new_due_at"] == moved.isoformat()

    def test_due_date_appearing_is_a_change(self) -> None:
        events = diff_assignment(state(due_at=None), state(), bootstrapped=True)
        assert types(events) == [EventType.due_date_changed]

    def test_points_change(self) -> None:
        events = diff_assignment(state(), state(points_possible=50.0), bootstrapped=True)
        assert types(events) == [EventType.points_changed]

    def test_workflow_state_change(self) -> None:
        events = diff_assignment(state(), state(workflow_state="unpublished"), bootstrapped=True)
        assert types(events) == [EventType.state_changed]

    def test_grade_posted(self) -> None:
        events = diff_assignment(state(), state(score=18.0), bootstrapped=True)
        assert types(events) == [EventType.grade_posted]
        assert events[0].payload["score"] == 18.0

    def test_grade_revised_is_reported(self) -> None:
        events = diff_assignment(state(score=15.0), state(score=18.0), bootstrapped=True)
        assert types(events) == [EventType.grade_posted]
        assert events[0].payload["previous_score"] == 15.0

    def test_grade_disappearing_is_not_reported(self) -> None:
        """An instructor un-posting grades is not news worth a notification."""
        assert diff_assignment(state(score=18.0), state(score=None), bootstrapped=True) == []

    def test_several_changes_at_once(self) -> None:
        events = diff_assignment(
            state(),
            state(due_at=DUE + timedelta(days=1), points_possible=80.0, score=70.0),
            bootstrapped=True,
        )
        assert set(types(events)) == {
            EventType.due_date_changed,
            EventType.points_changed,
            EventType.grade_posted,
        }


class TestTimezoneStability:
    def test_naive_and_aware_are_the_same_instant(self) -> None:
        """The bug this prevents: a phantom change event on every single sync pass.

        Postgres returns aware datetimes and Canvas sends Z-suffixed strings. If either
        side arrives naive, a plain comparison reports a change that did not happen.
        """
        naive = DUE.replace(tzinfo=None)
        assert diff_assignment(state(due_at=naive), state(due_at=DUE), bootstrapped=True) == []

    def test_same_instant_in_another_zone_is_not_a_change(self) -> None:
        toronto = DUE.astimezone(ZoneInfo("America/Toronto"))
        assert diff_assignment(state(due_at=toronto), state(due_at=DUE), bootstrapped=True) == []

    def test_both_none_is_not_a_change(self) -> None:
        assert diff_assignment(state(due_at=None), state(due_at=None), bootstrapped=True) == []


class TestRemovals:
    def test_disappearance_is_an_event(self) -> None:
        stored = {1: state(), 2: state(canvas_id=2, name="Quiz 3")}
        events = detect_removals(stored, {1}, bootstrapped=True)
        assert types(events) == [EventType.assignment_removed]
        assert events[0].canvas_id == 2

    def test_nothing_removed(self) -> None:
        stored = {1: state()}
        assert detect_removals(stored, {1}, bootstrapped=True) == []

    def test_removals_are_silent_before_bootstrap(self) -> None:
        assert detect_removals({1: state()}, set(), bootstrapped=False) == []
