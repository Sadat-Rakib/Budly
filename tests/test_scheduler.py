"""The local scheduler: missed-digest catch-up and restart deduplication (PRD §35-36).

Time is frozen at each scenario's moment; run_tick is replaced with a recorder so
these tests are about the scheduler's *decisions*, not the digest pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from canvasbuddy import scheduler as sched
from canvasbuddy.config import Settings
from canvasbuddy.models import Base, Digest


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "user_timezone": "America/Edmonton",
        "digest_slot": "daily@07:00",
        "nudge_slot": "daily@20:00",
        "review_slot": "",
        "checkin_slot": "",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class FrozenDatetime(datetime):
    """datetime with now() pinned, so the catch-up window is deterministic."""

    # Expressed in UTC: 08:20 in America/Edmonton on 2026-10-02.
    _frozen = datetime(2026, 10, 2, 14, 20, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        value = cls._frozen
        return value if tz is None else value.astimezone(tz)


@pytest.fixture
async def store(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def db_patch(monkeypatch: pytest.MonkeyPatch, store):
    """Point session_scope at the test's SQLite store."""
    _, sessionmaker = store

    import canvasbuddy.db as db_mod

    monkeypatch.setattr(db_mod, "get_sessionmaker", lambda: sessionmaker)
    return sessionmaker


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sched, "datetime", FrozenDatetime)
    return FrozenDatetime


@pytest.fixture
def tick_log(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Replace run_tick with a recorder of now_overrides."""
    calls: list[str | None] = []

    async def fake_run_tick(settings, tg_app=None, *, dry_run=False, now_override=None):
        calls.append(now_override)
        return {"ok": True}

    import canvasbuddy.notify.service as service

    monkeypatch.setattr(service, "run_tick", fake_run_tick)
    return calls


async def test_missed_morning_digest_is_caught_up(frozen_clock, db_patch, tick_log) -> None:
    """Scheduled 07:00, Budly starts 08:20, nothing sent today -> catch-up now."""
    await sched._catch_up(make_settings())
    assert tick_log == ["2026-10-02T07:00:00-06:00"]


async def test_already_sent_digest_is_not_resent(frozen_clock, db_patch, tick_log) -> None:
    """The dedupe key is (local_date, kind): a restart after a sent digest sends nothing."""
    sessionmaker = db_patch
    async with sessionmaker() as session:
        session.add(
            Digest(
                local_date=FrozenDatetime._frozen.date(),
                channel="telegram",
                kind="digest",
                body_md="x",
            )
        )
        await session.commit()

    await sched._catch_up(make_settings())
    assert tick_log == []


async def test_stale_morning_is_skipped_but_recent_evening_runs(
    frozen_clock, db_patch, tick_log
) -> None:
    """Budly starting at 23:00: the 20:00 nudge is 3h stale (inside the window,
    still caught up) but the 07:00 digest is 16h stale and stays dead."""
    frozen_clock._frozen = datetime(2026, 10, 3, 5, 0, tzinfo=UTC)  # 23:00 Edmonton
    await sched._catch_up(make_settings())
    assert tick_log == ["2026-10-02T20:00:00-06:00"]


async def test_evening_slot_catches_up_independently(frozen_clock, db_patch, tick_log) -> None:
    """Starting at 21:00: the 20:00 nudge is inside the window and runs; the
    07:00 digest is 14 hours stale and stays dead (PRD §35's no-stale rule)."""
    frozen_clock._frozen = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)  # 21:00 Edmonton
    await sched._catch_up(make_settings())
    assert tick_log == ["2026-10-02T20:00:00-06:00"]


async def test_scheduler_starts_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second start must not spawn a second loop (PRD §33)."""
    started: list[str] = []

    class FakeTask:
        def done(self):
            return False

        def cancel(self):
            return None

        def __await__(self):  # stop_scheduler awaits it
            return iter(())

    class FakeLoop:
        def create_task(self, coro, name=None):
            coro.close()  # never actually run it
            started.append(name)
            return FakeTask()

    monkeypatch.setattr(sched.asyncio, "get_running_loop", lambda: FakeLoop())
    sched.start_scheduler()
    sched.start_scheduler()
    assert len(started) == 1
    await sched.stop_scheduler()
