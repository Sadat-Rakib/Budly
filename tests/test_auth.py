"""The authorisation choke point.

A personal bot's token, once it leaks, gets probed. Everything below verifies that the
single `group=-1` handler drops foreign traffic before any other handler runs — including
handler types that do not exist yet.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from canvasbuddy.bot import _reject_strangers
from canvasbuddy.config import Settings

AUTHORISED = "5550001234"


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "canvas_base_url": "https://canvas.example.edu",
        "canvas_token": "t",
        "database_url": "postgresql://u:p@localhost/db",
        "telegram_bot_token": "123:abc",
        "telegram_chat_id": AUTHORISED,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def context(settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(application=SimpleNamespace(bot_data={"settings": settings}))


def update_from(chat_id: object) -> SimpleNamespace:
    chat = SimpleNamespace(id=chat_id) if chat_id is not None else None
    return SimpleNamespace(effective_chat=chat)


class TestRejectStrangers:
    async def test_authorised_chat_passes_through(self) -> None:
        await _reject_strangers(update_from(int(AUTHORISED)), context(make_settings()))

    async def test_chat_id_as_string_also_passes(self) -> None:
        """Telegram sends an int; the setting is a string. Compare as strings."""
        await _reject_strangers(update_from(AUTHORISED), context(make_settings()))

    async def test_foreign_chat_is_stopped(self) -> None:
        with pytest.raises(ApplicationHandlerStop):
            await _reject_strangers(update_from(999999), context(make_settings()))

    async def test_update_without_a_chat_is_stopped(self) -> None:
        """Fail closed: an update we cannot attribute is not one we act on."""
        with pytest.raises(ApplicationHandlerStop):
            await _reject_strangers(update_from(None), context(make_settings()))

    async def test_a_near_miss_is_stopped(self) -> None:
        """No prefix or substring matching — an id is either exact or it is not ours."""
        with pytest.raises(ApplicationHandlerStop):
            await _reject_strangers(update_from(555000123), context(make_settings()))
        with pytest.raises(ApplicationHandlerStop):
            await _reject_strangers(update_from(55500012340), context(make_settings()))

    async def test_negative_group_chat_id_is_stopped(self) -> None:
        """Someone adding the bot to a group must not gain access to the data."""
        with pytest.raises(ApplicationHandlerStop):
            await _reject_strangers(update_from(-1001234567890), context(make_settings()))


class TestWiring:
    def test_choke_point_runs_before_every_other_handler(self) -> None:
        """The guard must sit in a group that runs first, or it guards nothing."""
        from canvasbuddy.bot import build_application

        app = build_application(make_settings())
        groups = sorted(app.handlers)
        assert groups[0] == -1
        assert len(app.handlers[-1]) == 1
        assert app.handlers[-1][0].callback is _reject_strangers
        # Everything else lives in a later group, so the guard is always reached first.
        assert any(group > -1 for group in groups)

    def test_the_scheduled_jobs_are_actually_registered(self) -> None:
        """Without the [job-queue] extra, job_queue is None and the sync and digest
        silently never run while chat keeps working -- a failure with no symptom."""
        from canvasbuddy.bot import build_application

        app = build_application(make_settings())
        assert app.job_queue is not None, "python-telegram-bot[job-queue] is not installed"
        names = {job.callback.__name__ for job in app.job_queue.jobs()}
        assert {"job_sync", "job_digest"} <= names
