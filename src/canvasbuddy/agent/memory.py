"""Conversation memory.

Stores the visible turns of the conversation -- what was asked and what was answered --
and replays a bounded window of them. Tool traffic is deliberately *not* persisted: it is
large, it is only meaningful within the turn that produced it, and replaying stale tool
results invites the model to answer from data that has since changed.

Once the conversation outgrows the window, the older turns are folded into a single
summary rather than dropped, so the bot can still be asked about something from earlier.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import LLMError, OpenRouterClient
from canvasbuddy.models import ChatMessage

log = logging.getLogger(__name__)

_SUMMARY_ROLE = "summary"

_SUMMARY_INSTRUCTION = (
    "Summarise this conversation between a student and their coursework assistant in at "
    "most 120 words. Keep concrete facts: courses discussed, deadlines mentioned, "
    "decisions made, anything the student said about their own plans or preferences. "
    "Drop pleasantries. Write it as notes, not prose."
)


async def load_history(
    session: AsyncSession, settings: Settings, channel: str
) -> list[dict[str, Any]]:
    """The conversation so far, as chat messages: an optional summary, then recent turns."""
    recent = list(
        reversed(
            (
                await session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.channel == channel, ChatMessage.role != _SUMMARY_ROLE)
                    .order_by(ChatMessage.id.desc())
                    .limit(settings.agent_history_turns)
                )
            ).all()
        )
    )

    messages: list[dict[str, Any]] = []
    summary = await _current_summary(session, channel)
    if summary:
        messages.append({"role": "system", "content": f"Earlier in this conversation:\n{summary}"})
    messages.extend({"role": row.role, "content": row.content} for row in recent)
    return messages


async def save_turn(session: AsyncSession, channel: str, role: str, content: str) -> None:
    session.add(ChatMessage(channel=channel, role=role, content=content))


async def _current_summary(session: AsyncSession, channel: str) -> str | None:
    row = await session.scalar(
        select(ChatMessage)
        .where(ChatMessage.channel == channel, ChatMessage.role == _SUMMARY_ROLE)
        .order_by(ChatMessage.id.desc())
        .limit(1)
    )
    return row.content if row else None


async def maybe_summarize(
    session: AsyncSession,
    settings: Settings,
    llm: OpenRouterClient,
    channel: str,
) -> None:
    """Fold everything older than the replay window into a summary.

    Called after a turn completes, so its cost never sits in the user's response time. A
    failure here is logged and swallowed: losing the summary degrades memory, but failing
    the conversation over it would be worse.
    """
    covered_through = 0
    summary_row = await session.scalar(
        select(ChatMessage)
        .where(ChatMessage.channel == channel, ChatMessage.role == _SUMMARY_ROLE)
        .order_by(ChatMessage.id.desc())
        .limit(1)
    )
    if summary_row is not None:
        covered_through = (summary_row.tool_calls or {}).get("covered_through", 0)

    window = list(
        (
            await session.scalars(
                select(ChatMessage.id)
                .where(ChatMessage.channel == channel, ChatMessage.role != _SUMMARY_ROLE)
                .order_by(ChatMessage.id.desc())
                .limit(settings.agent_history_turns)
            )
        ).all()
    )
    if len(window) < settings.agent_history_turns:
        return

    oldest_kept = min(window)
    stale = list(
        (
            await session.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.channel == channel,
                    ChatMessage.role != _SUMMARY_ROLE,
                    ChatMessage.id < oldest_kept,
                    ChatMessage.id > covered_through,
                )
                .order_by(ChatMessage.id)
            )
        ).all()
    )
    if not stale:
        return

    transcript = "\n".join(f"{row.role}: {row.content}" for row in stale)
    previous = summary_row.content if summary_row else ""
    body = (f"Previous summary:\n{previous}\n\n" if previous else "") + f"New turns:\n{transcript}"

    try:
        choice = await llm.chat(
            [
                {"role": "system", "content": _SUMMARY_INSTRUCTION},
                {"role": "user", "content": body},
            ],
            tools=None,
            max_tokens=400,
        )
    except LLMError as exc:
        log.warning("Could not summarise conversation history: %s", exc)
        return

    text = (choice.get("message", {}).get("content") or "").strip()
    if not text:
        return

    session.add(
        ChatMessage(
            channel=channel,
            role=_SUMMARY_ROLE,
            content=text,
            # The watermark lives here rather than in a new column: `tool_calls` is JSONB,
            # unused for summaries, and this avoids a migration for one integer.
            tool_calls={"covered_through": max(row.id for row in stale)},
        )
    )
