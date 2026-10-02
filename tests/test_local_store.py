"""The local-first storage path: the whole pipeline on SQLite.

Budly v1.0 runs on a local SQLite file with no external services. This module
exercises the strongest version of that promise: the fixture Canvas (mock mode)
syncs into a real SQLite store through the production sync worker, and the chat
answers from it -- no Postgres, no fakes under the code under test.

The UTCDateTime column type is under the microscope here: SQLite stores naive
ISO strings, and every value read back must come with its UTC timezone
reattached, or every digest and every "due tomorrow" quietly drifts.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from canvasbuddy.canvas.client import open_canvas_client
from canvasbuddy.config import Settings
from canvasbuddy.models import Assignment, Base, Course, Event
from canvasbuddy.notify.service import sync_all_safe
from canvasbuddy.web.chat import answer_message


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "canvas_mock_mode": True,
        "database_url": "sqlite+aiosqlite://",  # replaced per-test below
        "user_timezone": "America/Edmonton",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


@pytest.fixture
async def store(tmp_path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'budly.db').as_posix()}"
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def settings(store, tmp_path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'budly.db').as_posix()}"
    return make_settings(database_url=database_url)


class TestLocalStorePipeline:
    async def test_mock_sync_persists_to_sqlite(self, store, settings) -> None:
        engine, sessionmaker = store
        async with open_canvas_client(settings) as client:
            async with sessionmaker() as session:
                report = await sync_all_safe(session, client, settings)
                await session.commit()

        assert report.courses_tracked == 3
        assert report.assignments_upserted == 10
        assert report.announcements_upserted == 3
        assert report.events == []  # first pass bootstraps: changes suppressed

        from sqlalchemy import select

        async with sessionmaker() as session:
            courses = (await session.scalars(select(Course))).all()
        assert {c.short_code for c in courses} == {"CSC", "COMP", "MATH"}

    async def test_timestamps_come_back_timezone_aware(self, store, settings) -> None:
        """SQLite drops the offset on storage; the column type must restore UTC."""
        _, sessionmaker = store
        async with open_canvas_client(settings) as client:
            async with sessionmaker() as session:
                await sync_all_safe(session, client, settings)
                await session.commit()

        from sqlalchemy import select

        async with sessionmaker() as session:
            assignment = (
                await session.scalars(
                    select(Assignment).where(Assignment.canvas_id == 990103)
                )
            ).first()

        assert assignment is not None and assignment.due_at is not None
        assert assignment.due_at.tzinfo is not None
        assert assignment.due_at.utcoffset().total_seconds() == 0

    async def test_second_sync_tells_the_change_story_on_sqlite(self, store, settings) -> None:
        _, sessionmaker = store
        async with open_canvas_client(settings) as client:
            async with sessionmaker() as session:
                await sync_all_safe(session, client, settings)
                await session.commit()
        # A second sync pass constructs a fresh client (as every pass does); the
        # worker itself tells it to apply the change story now that the bootstrap
        # is complete. No test-side nudging: this exercises the real handoff.
        async with open_canvas_client(settings) as client:
            async with sessionmaker() as session:
                await sync_all_safe(session, client, settings)
                await session.commit()

        from sqlalchemy import select

        async with sessionmaker() as session:
            rows = (await session.scalars(select(Event))).all()
        kinds = sorted(row.type.value for row in rows)
        assert kinds == ["due_date_changed", "new_announcement", "new_assignment"]

    async def test_chat_answers_from_the_local_store(self, store, settings) -> None:
        _, sessionmaker = store
        async with open_canvas_client(settings) as client:
            async with sessionmaker() as session:
                await sync_all_safe(session, client, settings)
                await session.commit()

        async with sessionmaker() as session:
            answer = await answer_message(session, settings, None, "What's due this week?")

        assert answer.kind == "due"
        assert "CSC" in answer.text
        assert answer.sources, "answers must carry source links"
        assert answer.sources[0].url.startswith("https://canvas.example.edu/")

    async def test_overdue_and_changes_from_the_local_store(self, store, settings) -> None:
        _, sessionmaker = store
        async with open_canvas_client(settings) as client:
            async with sessionmaker() as session:
                await sync_all_safe(session, client, settings)
                await session.commit()
            async with sessionmaker() as session:
                client.apply_scripted_changes()
                await sync_all_safe(session, client, settings)
                await session.commit()

        async with sessionmaker() as session:
            overdue = await answer_message(session, settings, None, "What's overdue?")
        assert overdue.kind == "overdue"
        assert "Lab 4: SQL Basics" in overdue.text

        async with sessionmaker() as session:
            changes = await answer_message(session, settings, None, "What's new since yesterday?")
        assert changes.kind == "changes"
        assert "deadline moved" in changes.text or "new assignment" in changes.text
