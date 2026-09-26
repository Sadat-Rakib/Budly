"""Regression: the Vercel webhook must build the chat agent.

PTB's ``Application.initialize()`` deliberately does not call ``post_init`` -- only
``run_polling()``/``run_webhook()`` do. ``api/index.py``'s ``get_tg()`` therefore has to
call it by hand; without that, ``bot_data["llm"]`` is never set and every conversational
reply fails on a perfectly good ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import api.index as api_index
import telegram

from canvasbuddy import bot
from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import OpenRouterClient


def _settings() -> Settings:
    return Settings(
        canvas_base_url="https://canvas.ualberta.ca",
        canvas_token="t",
        database_url="postgresql://u:p@localhost/db",
        user_timezone="America/Edmonton",
        telegram_bot_token="123456:abc",
        telegram_chat_id="42",
        openrouter_api_key="fake-key",
    )


async def _noop(*_args, **_kwargs):
    return None


class TestWebhookBuildsChatAgent:
    async def test_get_tg_runs_post_init(self, monkeypatch):
        s = _settings()
        monkeypatch.setattr(bot, "get_settings", lambda: s)
        monkeypatch.setattr(api_index, "get_settings", lambda: s)
        # Skip the live getMe call and the OpenRouter catalogue lookup.
        monkeypatch.setattr(telegram.Bot, "initialize", _noop)
        monkeypatch.setattr(OpenRouterClient, "assert_supports_tools", _noop)
        api_index._tg = None
        try:
            app = await api_index.get_tg()
            # The whole point of the fix: the agent is built on the webhook path.
            assert "llm" in app.bot_data
            assert isinstance(app.bot_data["llm"], OpenRouterClient)
            # And it is cached, so a warm instance serves the next request without re-init.
            assert await api_index.get_tg() is app
        finally:
            api_index._tg = None


class TestShutdownDisposesApplication:
    async def test_runs_post_shutdown_and_clears_cache(self):
        class Fake:
            def __init__(self) -> None:
                self.shutdown_with = None

            async def post_shutdown(self, app) -> None:
                self.shutdown_with = app

        fake = Fake()
        api_index._tg = fake
        try:
            await api_index._dispose_tg()
            assert api_index._tg is None
            assert fake.shutdown_with is fake
        finally:
            api_index._tg = None

    async def test_noop_when_never_built(self):
        api_index._tg = None
        await api_index._dispose_tg()
        assert api_index._tg is None

    async def test_survives_post_shutdown_raising(self):
        class Broken:
            async def post_shutdown(self, app) -> None:
                raise RuntimeError("boom")

        api_index._tg = Broken()
        try:
            await api_index._dispose_tg()  # must not propagate
            assert api_index._tg is None
        finally:
            api_index._tg = None
