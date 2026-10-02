"""The local Budly application: dashboard + scheduler on localhost.

This is what ``budly start`` runs. It is deliberately small: the dashboard API
router, the Bento page from ``public/``, and a lifespan that starts the
in-process scheduler and disposes the database engine on the way out.

It binds to 127.0.0.1 by default. Budly has no login on purpose -- the only
person who can reach a localhost-bound process is the person sitting at the
machine, whose Canvas token is already in the local configuration. Exposing
Budly to a network is a deliberate act (``--host 0.0.0.0``) and the operator
owns securing it; the CLI prints that warning.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from canvasbuddy.web.api import router as web_router

log = logging.getLogger(__name__)
# httpx logs request URLs at INFO; Telegram URLs embed the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

_PUBLIC_DIR = Path(__file__).resolve().parents[2] / "public"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from canvasbuddy.scheduler import start_scheduler, stop_scheduler

    await _ensure_local_store()
    start_scheduler()
    _initial_sync_task = asyncio.get_running_loop().create_task(_initial_sync())
    try:
        yield
    finally:
        await stop_scheduler()
        _initial_sync_task.cancel()
        from canvasbuddy.db import get_engine

        await get_engine().dispose()


async def _ensure_local_store() -> None:
    """Create tables on the local SQLite store; Postgres keeps using migrations."""
    from canvasbuddy.db import get_engine
    from canvasbuddy.models import Base

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _initial_sync() -> None:
    """First-run convenience: sync once in the background so the dashboard
    fills itself while the user reads the page. Cached data is always shown
    immediately; this only runs when the store is still empty."""
    from canvasbuddy.agent.tools import count_tracked_courses
    from canvasbuddy.config import get_settings
    from canvasbuddy.db import session_scope
    from canvasbuddy.notify.service import sync_now

    settings = get_settings()
    if not settings.canvas_configured and not settings.canvas_mock_mode:
        return
    try:
        async with session_scope() as session:
            if await count_tracked_courses(session) > 0:
                return
    except Exception:  # noqa: BLE001 - never block startup on a probe failure
        log.exception("could not check the local store")
        return
    log.info("no synced courses yet; running initial background sync")
    await sync_now(settings)


def create_local_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.include_router(web_router)
    app.mount("/", StaticFiles(directory=_PUBLIC_DIR, html=True, check_dir=False), name="public")
    return app
