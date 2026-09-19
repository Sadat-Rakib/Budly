"""Runtime state that a user can change from chat.

Environment variables are fixed at deploy time, so anything toggled from a Telegram
message -- a mute window, a watermark -- needs somewhere writable to live.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.models import Setting

MUTED_UNTIL = "muted_until"


async def get(session: AsyncSession, key: str) -> str | None:
    row = await session.get(Setting, key)
    return row.value if row else None


async def put(session: AsyncSession, key: str, value: str | None) -> None:
    row = await session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        session.add(row)
    row.value = value


async def mute_until(session: AsyncSession, until: datetime) -> None:
    await put(session, MUTED_UNTIL, until.astimezone(UTC).isoformat())


async def unmute(session: AsyncSession) -> None:
    await put(session, MUTED_UNTIL, None)


async def is_muted(session: AsyncSession, *, now: datetime | None = None) -> datetime | None:
    """The moment the mute expires, or None if not muted.

    Muting suppresses everything the bot *initiates* -- the digest, the nudge. It never
    stops it answering a direct question: silencing a reply to something you just asked
    would be a bug, not a feature.
    """
    raw = await get(session, MUTED_UNTIL)
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if until <= (now or datetime.now(UTC)):
        return None
    return until


async def already_sent_today(session: AsyncSession, key: str, local_date: str) -> bool:
    """Generic once-a-day guard for jobs that have no table of their own."""
    return await get(session, key) == local_date


async def mark_sent_today(session: AsyncSession, key: str, local_date: str) -> None:
    await put(session, key, local_date)
