"""Telegram + Slack notifiers (serverless-safe, no polling)."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import httpx
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from canvasbuddy.config import Settings
from canvasbuddy.digest.render import escape_md2
from canvasbuddy.notify.content import NotificationContent

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000  # under 4096, split on section boundaries
SLACK_LIMIT = 3400  # under ~3500-4000, split on section boundaries


class Notifier(Protocol):
    name: str

    def enabled(self, settings: Settings) -> bool: ...
    async def send(self, payload: str) -> str: ...


def escape_slack(text: str) -> str:
    """Escape dynamic text for Slack mrkdwn."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_chunks(blocks: list[str], limit: int) -> list[str]:
    """Join section blocks, splitting on boundaries so no chunk exceeds limit."""
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if not block:
            continue
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Single oversized block: hard-split (should be rare; caps keep it small).
            while len(block) > limit:
                chunks.append(block[:limit])
                block = block[limit:]
            current = block
    if current:
        chunks.append(current)
    return chunks or [""]


def render_telegram_chunks(content: NotificationContent) -> list[str]:
    """Render NotificationContent to MarkdownV2 chunks."""
    blocks: list[str] = [f"*{escape_md2(content.title)}*"]
    for sec in content.sections:
        if not sec.lines:
            continue
        body = "\n".join(f"• {escape_md2(line)}" for line in sec.lines)
        blocks.append(f"*{escape_md2(sec.heading)}*\n{body}")
    if content.footer:
        blocks.append(f"_{escape_md2(content.footer)}_")
    return _split_chunks(blocks, TELEGRAM_LIMIT)


def render_slack_chunks(content: NotificationContent) -> list[str]:
    """Render NotificationContent to Slack mrkdwn chunks."""
    blocks: list[str] = [f"*{escape_slack(content.title)}*"]
    for sec in content.sections:
        if not sec.lines:
            continue
        body = "\n".join(f"• {escape_slack(line)}" for line in sec.lines)
        blocks.append(f"*{escape_slack(sec.heading)}*\n{body}")
    if content.footer:
        blocks.append(f"_{escape_slack(content.footer)}_")
    return _split_chunks(blocks, SLACK_LIMIT)


def render_digest_slack_from_content(title: str, sections: list[tuple[str, list[str]]]) -> str:
    """Low-level helper: title + (heading, lines) -> single mrkdwn string."""
    parts = [f"*{escape_slack(title)}*"]
    for heading, lines in sections:
        if not lines:
            continue
        parts.append(
            f"*{escape_slack(heading)}*\n" + "\n".join(f"• {escape_slack(line)}" for line in lines)
        )
    return "\n\n".join(parts)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, settings: Settings, bot: Bot | None = None) -> None:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            raise RuntimeError("Telegram not configured")
        self._chat_id = settings.telegram_chat_id
        token = settings.telegram_bot_token.get_secret_value()
        self._bot = bot or Bot(token=token)

    def enabled(self, settings: Settings) -> bool:
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)

    async def send(self, payload: str) -> str:
        """Send one pre-rendered MarkdownV2 chunk, falling back to plain on 400."""
        try:
            msg = await self._bot.send_message(
                chat_id=self._chat_id, text=payload, parse_mode=ParseMode.MARKDOWN_V2
            )
            return str(msg.message_id)
        except TelegramError as exc:
            log.warning("Telegram MarkdownV2 rejected, falling back to plain: %s", exc)
            msg = await self._bot.send_message(chat_id=self._chat_id, text=payload)
            # Return plain id; caller treats as sent. Strip formatting next time via logs.
            return str(msg.message_id)

    async def send_content(self, content: NotificationContent) -> str:
        chunks = render_telegram_chunks(content)
        last = "0"
        for chunk in chunks:
            # For fallback: try MarkdownV2 first, else plain (send() handles it).
            # If MarkdownV2 fails due to escaping bug, send plain stripped version.
            try:
                last = await self.send(chunk)
            except TelegramError:
                from canvasbuddy.digest.render import strip_markdown

                msg = await self._bot.send_message(
                    chat_id=self._chat_id, text=strip_markdown(chunk)
                )
                last = str(msg.message_id)
        return last


class SlackNotifier:
    name = "slack"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.slack_webhook_url:
            raise RuntimeError("Slack not configured")
        self._url = settings.slack_webhook_url.get_secret_value()
        self._client = client
        self._owns = client is None

    def enabled(self, settings: Settings) -> bool:
        return settings.slack_webhook_url is not None

    async def send(self, payload: str) -> str:
        """POST mrkdwn text to Incoming Webhook with retries on 5xx/429."""
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        try:
            last_exc: Exception | None = None
            for attempt in range(3):
                resp = await client.post(self._url, json={"text": payload})
                if resp.status_code == 200:
                    return "ok"
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"Slack {resp.status_code}: {resp.text[:200]}")
                    retry_after = resp.headers.get("Retry-After")
                    delay = (
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else (attempt + 1)
                    )
                    await asyncio.sleep(min(delay, 10))
                    continue
                raise RuntimeError(f"Slack webhook failed {resp.status_code}: {resp.text[:400]}")
            raise last_exc or RuntimeError("Slack send failed")
        finally:
            if self._owns:
                await client.aclose()

    async def send_content(self, content: NotificationContent) -> str:
        chunks = render_slack_chunks(content)
        for chunk in chunks:
            await self.send(chunk)
        return "ok"
