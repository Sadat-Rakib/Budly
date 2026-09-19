"""Telegram, send-only.

P0 has nothing to listen for, so there is no webhook, no TLS certificate, and no
public hostname to arrange. When P1 adds the chat agent, long polling is the better
fit for a single-user bot than a webhook -- python-telegram-bot's own guidance is that
switching to webhooks needs a reason beyond novelty.
"""

from __future__ import annotations

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from canvasbuddy.channels.base import Button
from canvasbuddy.config import Settings

log = logging.getLogger(__name__)


class TelegramNotConfiguredError(RuntimeError):
    pass


class TelegramChannel:
    name = "telegram"

    def __init__(self, settings: Settings) -> None:
        if not settings.telegram_configured:
            raise TelegramNotConfiguredError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set. "
                "Create a bot with @BotFather, message it once, then read the chat id "
                "from https://api.telegram.org/bot<TOKEN>/getUpdates"
            )
        assert settings.telegram_bot_token is not None
        self._chat_id = settings.telegram_chat_id
        self._bot = Bot(token=settings.telegram_bot_token.get_secret_value())

    async def send(self, text: str, buttons: list[Button] | None = None) -> str:
        markup = (
            InlineKeyboardMarkup(
                [[InlineKeyboardButton(b.label, callback_data=b.callback_data)] for b in buttons]
            )
            if buttons
            else None
        )
        try:
            message = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=markup,
                disable_notification=False,
            )
        except TelegramError as exc:
            # Almost always an unescaped MarkdownV2 character. Log the payload so the
            # offending line is identifiable without reproducing the whole sync.
            log.error("Telegram rejected the message: %s\n---\n%s\n---", exc, text)
            raise
        return str(message.message_id)

    async def send_plain(self, text: str) -> str:
        """Escape-hatch for alerts, where a formatting failure must not swallow the alert."""
        message = await self._bot.send_message(chat_id=self._chat_id, text=text)
        return str(message.message_id)
