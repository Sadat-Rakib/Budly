"""The fixture Canvas (``CANVAS_MOCK_MODE``): fixtures, gating, and the demo story.

The fixtures are the product's answer to "I want to develop without a Canvas
account". Everything here runs without a database: the world is plain dicts shaped
like the Canvas API, the mock client is exercised directly, and the sync's own
Pydantic schemas are the judge of whether the fixtures are realistic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from canvasbuddy.canvas import schemas
from canvasbuddy.canvas.client import CanvasClient, open_canvas_client
from canvasbuddy.canvas.mock import MOCK_ID_FLOOR, MockCanvasClient, build_fixture_world
from canvasbuddy.config import Settings


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


FIXTURE_TODAY = datetime(2026, 10, 1, tzinfo=UTC).date()


def world():
    return build_fixture_world("2026 Fall", "https://canvas.example.edu", today=FIXTURE_TODAY)


class TestFixtureWorld:
    def test_every_row_validates_against_the_canvas_schemas(self) -> None:
        """Realism is enforced, not assumed: the sync's own Pydantic models must
        accept every fixture row exactly as the sync worker would feed them in."""
        w = world()

        for raw in w.courses:
            parsed = schemas.Course.model_validate(raw)
            assert parsed.term is not None and parsed.term.name == "2026 Fall"
        for rows in w.assignments.values():
            for raw in rows:
                assert schemas.Assignment.model_validate(raw).id >= MOCK_ID_FLOOR
        for raw in w.announcements:
            parsed = schemas.Announcement.model_validate(raw)
            assert parsed.course_canvas_id in {990001, 990002, 990003}
            assert parsed.body_text  # html flattened, not empty
        for course_id in w.enrollments:
            for raw in w.enrollments[course_id]:
                assert schemas.Enrollment.model_validate(raw).course_section_id
            for raw in w.sections[course_id]:
                schemas.Section.model_validate(raw)
        for raw in w.planner:
            item = schemas.PlannerItem.model_validate(raw)
            assert item.plannable_type == "assignment"

    def test_three_courses_with_the_full_story(self) -> None:
        w = world()
        assert len(w.courses) == 3

        all_assignments = [row for rows in w.assignments.values() for row in rows]
        today = datetime.combine(FIXTURE_TODAY, datetime.min.time(), tzinfo=UTC)

        # A completed and graded item...
        completed = [a for a in all_assignments if (a.get("submission") or {}).get("score")]
        assert completed and all(a["submission"]["workflow_state"] == "graded" for a in completed)
        # ...an overdue unsubmitted item...
        overdue = [
            a
            for a in all_assignments
            if (
                a["due_at"]
                and datetime.fromisoformat(a["due_at"]) < today
                and not a["submission"].get("submitted_at")
            )
        ]
        assert overdue
        # ...and undated graded work (the easy-to-miss kind).
        assert [a for a in all_assignments if a["due_at"] is None and a["points_possible"]]

    def test_term_matches_the_deployment_setting(self) -> None:
        """Fixture courses must pass the tracked-term filter whatever term the
        developer has configured -- otherwise mock mode syncs nothing."""
        w = build_fixture_world("Fall Term 2026", "https://canvas.example.edu")
        assert {c["term"]["name"] for c in w.courses} == {"Fall Term 2026"}


class TestMockClient:
    def make_client(self) -> MockCanvasClient:
        return MockCanvasClient(make_settings())

    async def test_endpoints_mirror_the_sync_calls(self) -> None:
        client = self.make_client()
        courses = await client.get_courses()
        assert {c["id"] for c in courses} == {990001, 990002, 990003}

        assignments = await client.get_assignments(990001)
        assert all(a["id"] >= MOCK_ID_FLOOR for a in assignments)

        planner = await client.get_planner_items(
            datetime(2026, 9, 1, tzinfo=UTC).date(),
            datetime(2026, 11, 1, tzinfo=UTC).date(),
            ["course_990001"],
        )
        expected_ids = {990101, 990102, 990103, 990104, 990105}
        assert planner and all(p["plannable_id"] in expected_ids for p in planner)

        news = await client.get_announcements(
            ["course_990002"],
            (datetime.now(UTC) - timedelta(days=1)).date(),
            (datetime.now(UTC) + timedelta(days=1)).date(),
        )
        assert [a["id"] for a in news] == [990502]

    async def test_second_pass_tells_the_change_story(self) -> None:
        """Sync one bootstraps; sync two moves a deadline, adds work and posts
        news -- the events change detection exists to report."""
        client = self.make_client()
        before = {a["id"]: a for a in await client.get_assignments(990002)}
        count_before = len(await client.get_assignments(990003))

        client.apply_scripted_changes()  # the worker calls this after bootstrap

        after = {a["id"]: a for a in await client.get_assignments(990002)}
        assert after[990201]["due_at"] != before[990201]["due_at"]

        math_after = await client.get_assignments(990003)
        assert len(math_after) == count_before + 1  # Problem Set 7 appeared

        news = await client.get_announcements(
            ["course_990001"],
            (datetime.now(UTC) - timedelta(days=1)).date(),
            (datetime.now(UTC) + timedelta(days=1)).date(),
        )
        assert any(a["id"] == 990504 for a in news)

        # Idempotent: a third pass does not move the deadline again.
        await client.get_courses()
        third = {a["id"]: a for a in await client.get_assignments(990002)}
        assert third[990201]["due_at"] == after[990201]["due_at"]

    async def test_unimplemented_endpoints_fail_honestly(self) -> None:
        client = self.make_client()
        try:
            await client.get_files(990001)
        except NotImplementedError:
            return
        raise AssertionError("get_files should not exist on the mock")


class TestGating:
    def test_factory_returns_mock_only_when_flagged(self) -> None:
        mocked = open_canvas_client(make_settings(canvas_mock_mode=True))
        assert isinstance(mocked, MockCanvasClient)
        assert isinstance(open_canvas_client(make_settings()), CanvasClient)

    def test_mock_never_runs_silently(self) -> None:
        """The client logs a warning on every construction -- the code path a
        production deployment would trip over if the flag were ever set there."""
        import logging

        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Capture()
        logging.getLogger("canvasbuddy.canvas.mock").addHandler(handler)
        try:
            MockCanvasClient(make_settings(canvas_mock_mode=True))
        finally:
            logging.getLogger("canvasbuddy.canvas.mock").removeHandler(handler)

        assert any(
            record.levelno >= logging.WARNING and "MOCK" in record.getMessage()
            for record in records
        )
