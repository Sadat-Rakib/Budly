"""Legacy Vercel entrypoint: Telegram webhook + cron tick.

Budly v1.0 is local-first: the dashboard and scheduler run on the user's machine
(`budly start`). This function only serves the optional hosted Telegram webhook
and the legacy cron tick for deployments that still use them, plus the static
showcase page. The dashboard API deliberately does NOT run here -- a public
endpoint has no business serving someone's Canvas data.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from telegram import Update  # noqa: E402

from canvasbuddy import bot  # noqa: E402
from canvasbuddy.config import get_settings  # noqa: E402
from canvasbuddy.notify.service import run_tick  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def _dispose_tg() -> None:
    """Dispose the DB engine and LLM client.

    Best-effort: serverless instances may be frozen rather than shut down, so a failure
    here must not block anything.
    """
    global _tg
    if _tg is None:
        return
    try:
        if _tg.post_shutdown:
            await _tg.post_shutdown(_tg)
    except Exception:
        logging.getLogger(__name__).warning("post_shutdown failed", exc_info=True)
    _tg = None


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup stays lazy: building the Telegram app on every cold start would also make
    # /api/health and /api/cron pay for a getMe they do not need.
    yield
    await _dispose_tg()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

_tg = None
_lock = asyncio.Lock()


async def get_tg():
    global _tg
    if _tg is None:
        async with _lock:
            if _tg is None:
                tg = bot.build_application()
                await tg.initialize()
                # PTB's initialize() deliberately does not run post_init -- only
                # run_polling()/run_webhook() do. On the webhook path it has to be
                # called by hand, or the OpenRouter client is never built and every
                # conversational reply fails with "OPENROUTER_API_KEY isn't set".
                if tg.post_init:
                    await tg.post_init(tg)
                _tg = tg
    return _tg



def _check(provided: str | None, env: str, prefix: str = "") -> None:
    expected = os.environ.get(env)
    if not expected:
        raise HTTPException(500, f"{env} not configured")
    if not hmac.compare_digest((provided or "").encode(), (prefix + expected).encode()):
        raise HTTPException(403, "forbidden")


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.post("/api/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    _check(x_telegram_bot_api_secret_token, "WEBHOOK_SECRET")
    tg = await get_tg()
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "invalid JSON") from None
    await tg.process_update(Update.de_json(payload, tg.bot))
    return {"ok": True}


@app.get("/api/cron")
async def cron(
    authorization: str | None = Header(default=None),
    dry_run: bool = False,
    now: str | None = None,
):
    _check(authorization, "CRON_SECRET", prefix="Bearer ")
    return await run_tick(get_settings(), await get_tg(), dry_run=dry_run, now_override=now)


# Static files (the showcase page, downloads) are served by Vercel's own static
# layer from public/ -- they win before the function. The function therefore mounts
# no static directory: anything unmatched is a plain 404, and the function bundle
# does not need the page at all.
