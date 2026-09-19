"""Vercel entrypoint: Telegram webhook + cron tick."""

from __future__ import annotations

import asyncio
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
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_tg = None
_lock = asyncio.Lock()


async def get_tg():
    global _tg
    if _tg is None:
        async with _lock:
            if _tg is None:
                tg = bot.build_application()
                await tg.initialize()
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
    await tg.process_update(Update.de_json(await request.json(), tg.bot))
    return {"ok": True}


@app.get("/api/cron")
async def cron(
    authorization: str | None = Header(default=None),
    dry_run: bool = False,
    now: str | None = None,
):
    _check(authorization, "CRON_SECRET", prefix="Bearer ")
    return await run_tick(get_settings(), await get_tg(), dry_run=dry_run, now_override=now)
