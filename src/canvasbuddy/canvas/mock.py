"""A fixture Canvas for local development: ``CANVAS_MOCK_MODE=true``.

The mock exists so a developer (or a demo) can run the whole product -- sync,
change detection, digests, dashboard chat -- with no Canvas account at all. It
implements the same surface as :class:`~canvasbuddy.canvas.client.CanvasClient`
for the endpoints the sync uses, and answers from a small, deterministic world
built at startup.

Design choices worth naming:

* **Dates are relative to "now".** An overdue item is always 8 days past, a
  "due tomorrow" item is always due tomorrow. Static dates would silently rot
  until the fixtures describe a semester that ended years ago.
* **The second sync changes things.** The first pass bootstraps (change events
  suppressed, as with a real course); the second pass moves a deadline, adds an
  assignment and posts an announcement, so change detection, "what's new?" and
  the evening digest all have something true to report.
* **It is loud.** Every construction logs a warning, sync results carry
  ``mock: true``, and the dashboard shows a Demo data badge. A mock that could
  pass unnoticed in production would be a liability.
* Course ids live in the 990000+ range, clearly outside any real Canvas id
  space this project is likely to meet.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from datetime import date as date_t

UTC = UTC

log = logging.getLogger(__name__)

#: All fixture ids live at or above this line, so real and fake data can never
#: be confused by looking at an id alone.
MOCK_ID_FLOOR = 990000

_ANNOUNCE = time(10, 0)  # instructors post in the morning
_DEADLINE = time(23, 59)  # and things are due at midnight


def _at(day: date_t, when: time = _DEADLINE) -> datetime:
    return datetime.combine(day, when, tzinfo=UTC)


@dataclass
class FixtureWorld:
    """The mock Canvas, as plain dicts shaped exactly like the API's JSON."""

    courses: list[dict] = field(default_factory=list)
    enrollments: dict[int, list[dict]] = field(default_factory=dict)
    sections: dict[int, list[dict]] = field(default_factory=dict)
    assignments: dict[int, list[dict]] = field(default_factory=dict)
    planner: list[dict] = field(default_factory=list)
    announcements: list[dict] = field(default_factory=list)


def build_fixture_world(
    term_name: str,
    base_url: str,
    *,
    today: date_t | None = None,
) -> FixtureWorld:
    """Three tracked courses with the full cast: due today, due tomorrow, later
    work, an overdue item, a completed and graded item, and undated graded work.

    ``term_name`` is the deployment's configured term, so tracked-course
    filtering passes no matter what term a developer has set.
    """
    today = today or datetime.now(UTC).date()
    term = {
        "id": MOCK_ID_FLOOR + 900,
        "name": term_name,
        "start_at": _at(today - timedelta(days=45), time(0, 0)),
        "end_at": _at(today + timedelta(days=45), time(0, 0)),
    }

    def course(canvas_id: int, code: str, title: str) -> dict:
        return {
            "id": canvas_id,
            "name": f"{code}: {title}",
            "course_code": code,
            "syllabus_body": f"<p>{title}. Weekly labs, two midterms, one final.</p>",
            "term": dict(term),
        }

    def assignment(
        canvas_id: int,
        course_id: int,
        name: str,
        *,
        due: datetime | None,
        points: float,
        submitted: bool = False,
        score: float | None = None,
    ) -> dict:
        row = {
            "id": canvas_id,
            "name": name,
            "description": f"<p>{name} — fixture description.</p>",
            "due_at": due.isoformat() if due else None,
            "points_possible": points,
            "submission_types": ["online_upload"],
            "html_url": f"{base_url}/courses/{course_id}/assignments/{canvas_id}",
            "workflow_state": "published",
        }
        if submitted:
            row["submission"] = {
                "workflow_state": "graded" if score is not None else "submitted",
                "submitted_at": (due - timedelta(days=1)).isoformat() if due else None,
                "score": score,
                "grade": f"{score:g}" if score is not None else None,
                "graded_at": (due + timedelta(days=2)).isoformat() if due else None,
            }
        else:
            row["submission"] = {"workflow_state": "unsubmitted"}
        return row

    world = FixtureWorld()

    # -- courses -----------------------------------------------------------
    world.courses = [
        course(990001, "AUSTA 153H3", "Introduction to Data Analysis"),
        course(990002, "COMP 214H3", "Databases and Web Applications"),
        course(990003, "MATH 120H3", "Calculus II"),
    ]
    for index, canvas_id in enumerate((990001, 990002, 990003), start=1):
        code = world.courses[index - 1]["course_code"]
        section_id = MOCK_ID_FLOOR + 700 + index
        world.sections[canvas_id] = [
            {"id": section_id, "name": f"{code} LEC A01"},
            {"id": section_id + 50, "name": f"{code} TUT B02"},
        ]
        world.enrollments[canvas_id] = [
            {
                "id": MOCK_ID_FLOOR + 800 + index,
                "type": "StudentEnrollment",
                "course_section_id": section_id,
                "enrollment_state": "active",
            }
        ]

    # -- assignments ---------------------------------------------------------
    # AUSTA 153: the full story — overdue, due today, due this week, completed
    # and graded, plus undated graded work.
    world.assignments[990001] = [
        assignment(
            990101, 990001, "Assignment 1: Survey Analysis",
            due=_at(today - timedelta(days=20)), points=40, submitted=True, score=36,
        ),
        assignment(
            990102, 990001, "Lab 4: SQL Basics",
            due=_at(today - timedelta(days=8)), points=10,
        ),
        assignment(
            990103, 990001, "Lab 5: Data Cleaning",
            due=_at(today), points=10,
        ),
        assignment(
            990104, 990001, "Assignment 2: Regression Report",
            due=_at(today + timedelta(days=3)), points=40,
        ),
        assignment(
            990105, 990001, "Quiz 3: Distributions",
            due=_at(today + timedelta(days=7)), points=15,
        ),
        assignment(
            990106, 990001, "Reading Response 4",
            due=None, points=5,
        ),
    ]
    # COMP 214: a near deadline and the mid-term story.
    world.assignments[990002] = [
        assignment(
            990201, 990002, "Group Project Milestone 2",
            due=_at(today + timedelta(days=1)), points=25,
        ),
        assignment(
            990202, 990002, "Lab 7: Indexes and Queries",
            due=_at(today + timedelta(days=5)), points=10,
        ),
    ]
    # MATH 120: light week, one already done.
    world.assignments[990003] = [
        assignment(
            990301, 990003, "Problem Set 6",
            due=_at(today + timedelta(days=2)), points=20,
        ),
        assignment(
            990302, 990003, "WebWork 5",
            due=_at(today - timedelta(days=3)), points=10, submitted=True, score=9,
        ),
    ]

    # -- planner overlay ------------------------------------------------------
    for course_id, rows in world.assignments.items():
        course_row = next(c for c in world.courses if c["id"] == course_id)
        for row in rows:
            if row["due_at"] is None:
                continue  # the real planner drops undated items too
            world.planner.append(
                {
                    "plannable_id": row["id"],
                    "plannable_type": "assignment",
                    "plannable_date": row["due_at"],
                    "context_name": course_row["name"],
                    "submissions": {
                        "submitted": bool(row.get("submission", {}).get("submitted_at"))
                    },
                }
            )

    # -- announcements ---------------------------------------------------------
    def announce(canvas_id: int, course_id: int, title: str, hours_ago: int) -> dict:
        return {
            "id": canvas_id,
            "title": title,
            "message": f"<p>{title} — see the course page for details.</p>",
            "posted_at": (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(),
            "html_url": f"{base_url}/courses/{course_id}/discussions/{canvas_id}",
            "context_code": f"course_{course_id}",
            "user_name": "Dr. Ada Example",
        }

    world.announcements = [
        announce(990501, 990001, "Module 6 released", hours_ago=26),
        announce(990502, 990002, "Midterm room change: CCIS 1-140", hours_ago=3),
        announce(990503, 990003, "Office hours moved to Thursday", hours_ago=72),
    ]

    return world


class MockCanvasClient:
    """Drop-in stand-in for CanvasClient, backed by :data:`FixtureWorld` dicts.

    Implements exactly the endpoints the sync worker uses. Anything else raises
    ``NotImplementedError`` with a message naming the mock, so a stray call fails
    honestly instead of silently hitting a half-built fake.
    """

    mock = True

    def __init__(self, settings) -> None:
        # Fixture links always point at the placeholder host: they must look like
        # Canvas links without ever resolving into someone's real Canvas.
        self.world = build_fixture_world(settings.canvas_term, "https://canvas.example.edu")
        self._changes_applied = False
        log.warning(
            "CANVAS MOCK MODE is ON — serving built-in fixtures (course ids >= %d), "
            "no real Canvas calls. Never enable this on a production deployment.",
            MOCK_ID_FLOOR,
        )

    async def __aenter__(self) -> MockCanvasClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def __getattr__(self, name: str):
        """Any endpoint the sync does not use fails loudly and names the mock.

        A stray call landing on a half-built fake is worse than an exception.
        """

        def _not_mocked(*args: object, **kwargs: object) -> None:
            raise NotImplementedError(
                f"MockCanvasClient does not implement {name!r} -- "
                "it only covers the endpoints the sync worker uses."
            )

        return _not_mocked

    # -- scripted change story -------------------------------------------------

    def apply_scripted_changes(self) -> None:
        """The demo story: after the first sync, the term moves on.

        Called by the sync worker once the database shows a completed bootstrap,
        so "sync twice" tells the change story reliably across restarts and
        serverless cold starts -- driven by real state, not a process counter.

        A deadline moves, a new assignment appears, and an announcement lands —
        exactly the events change detection and the digests exist to report.
        """
        if self._changes_applied:
            return
        self._changes_applied = True

        milestone = next(
            row for row in self.world.assignments[990002] if row["id"] == 990201
        )
        moved = datetime.fromisoformat(milestone["due_at"]) + timedelta(days=2)
        milestone["due_at"] = moved.isoformat()
        milestone["name"] = "Group Project Milestone 2 (extended)"

        self.world.assignments[990003].append(
            {
                "id": 990303,
                "name": "Problem Set 7",
                "description": "<p>Problem Set 7 — fixture description.</p>",
                "due_at": _at(datetime.now(UTC).date() + timedelta(days=6)).isoformat(),
                "points_possible": 20,
                "submission_types": ["online_upload"],
                "html_url": "https://canvas.example.edu/courses/990003/assignments/990303",
                "workflow_state": "published",
                "submission": {"workflow_state": "unsubmitted"},
            }
        )
        self.world.planner.append(
            {
                "plannable_id": 990303,
                "plannable_type": "assignment",
                "plannable_date": _at(datetime.now(UTC).date() + timedelta(days=6)),
                "context_name": "MATH 120H3: Calculus II",
                "submissions": {"submitted": False},
            }
        )
        self.world.announcements.append(
            {
                "id": 990504,
                "title": "Midterm review session added",
                "message": "<p>Midterm review session added — Friday at 3pm.</p>",
                "posted_at": datetime.now(UTC).isoformat(),
                "html_url": "https://canvas.example.edu/courses/990001/discussions/990504",
                "context_code": "course_990001",
                "user_name": "Dr. Ada Example",
            }
        )
        log.info("Mock Canvas: applied scripted term changes (deadline move, new work, news)")

    # -- endpoints -------------------------------------------------------------

    async def get_self(self) -> dict:
        return {"id": MOCK_ID_FLOOR, "name": "Demo Student"}

    async def get_courses(self) -> list[dict]:
        return copy.deepcopy(self.world.courses)

    async def get_enrollments(self, course_id: int) -> list[dict]:
        return copy.deepcopy(self.world.enrollments[course_id])

    async def get_sections(self, course_id: int) -> list[dict]:
        return copy.deepcopy(self.world.sections[course_id])

    async def get_assignments(self, course_id: int) -> list[dict]:
        return copy.deepcopy(self.world.assignments[course_id])

    async def get_planner_items(self, start_date, end_date, context_codes=None) -> list[dict]:
        wanted_courses = (
            {int(code.removeprefix("course_")) for code in context_codes}
            if context_codes
            else set(self.world.assignments)
        )
        wanted_ids: set[int] = set()
        for course_id in wanted_courses:
            wanted_ids.update(row["id"] for row in self.world.assignments.get(course_id, []))
        def _plannable_day(row: dict) -> date_t:
            when = row["plannable_date"]
            if isinstance(when, datetime):
                return when.date()
            return datetime.fromisoformat(when).date()

        return [
            copy.deepcopy(row)
            for row in self.world.planner
            if row["plannable_id"] in wanted_ids and start_date <= _plannable_day(row) <= end_date
        ]

    async def get_announcements(self, context_codes, start_date, end_date) -> list[dict]:
        wanted = {int(code.removeprefix("course_")) for code in context_codes}
        start = start_date if isinstance(start_date, datetime) else datetime.combine(
            start_date, time(0, 0), tzinfo=UTC
        )
        end = end_date if isinstance(end_date, datetime) else datetime.combine(
            end_date, time(23, 59), tzinfo=UTC
        )
        return [
            copy.deepcopy(row)
            for row in self.world.announcements
            if int(row["context_code"].removeprefix("course_")) in wanted
            and start <= datetime.fromisoformat(row["posted_at"]) <= end
        ]
