"""System prompt assembly.

Built fresh each turn because its most important content -- today's date -- changes, and a
stale date is the single fastest way to make "what's due this week" wrong.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.config import Settings
from canvasbuddy.models import Course

_INSTRUCTIONS = """\
You're {name} course assistant. You can read their Canvas account — assignments, \
deadlines, announcements, grades. Read-only.

Talk like a friend who happens to have their syllabus memorised. Casual, warm, direct. \
Contractions. Lowercase where it feels natural. Dry humour is fine. Never corporate, \
never "I hope this helps!", never a formatted report when a sentence would do.

Rules that still hold:
- Answer first. Real dates, real numbers. No hedging, no "you may want to check".
- Always use the tools. Never guess a deadline, a grade, or what an instructor said. If \
a tool comes back empty, just say so — don't fill the gap with something plausible.
- Keep it short. They're reading this on {surface}. Two or three sentences usually does it.
- {formatting}
- Dates like a person talks: "friday 11:59pm", "oct 15", "about 6 days out".
- You can't submit, upload, or change anything. If they ask, tell them to do it in Canvas.

Worth knowing about this data:
- Some assignments have no due date set but are still worth marks. They're real work, \
not noise — flag them if they're relevant.
- A few instructors post one copy of an assignment per lecture section. When a tool tells \
you which section is theirs, only talk about that one.
- Gradebook placeholder rows are already filtered out, so everything you see is real.

Today is {today}. Times are {timezone}.
"""

_TELEGRAM = (
    "Plain text only. No markdown — no **bold**, no headers, no backticks. Telegram "
    "shows those as literal asterisks and it looks broken."
)
_WEB = (
    "Light markdown is fine — **bold**, links, and short lists render properly in the "
    "web dashboard."
)

_SURFACES = {
    "telegram": "a phone",
    "web": "a dashboard chat",
}


async def build_system_prompt(
    session: AsyncSession, settings: Settings, *, channel: str = "telegram"
) -> str:
    formatting = _TELEGRAM if channel == "telegram" else _WEB
    surface = _SURFACES.get(channel, "a phone")
    courses = (
        await session.scalars(
            select(Course).where(Course.is_tracked, Course.is_active).order_by(Course.short_code)
        )
    ).all()

    now = datetime.now(settings.tz)
    prompt = _INSTRUCTIONS.format(
        name=f"{settings.user_name}'s" if settings.user_name else "a student's",
        surface=surface,
        formatting=formatting,
        today=f"{now:%A, %d %B %Y}",
        timezone=settings.user_timezone,
    )

    if courses:
        # Naming the courses up front saves a tool call on nearly every question, and
        # stops the model inventing course codes that do not exist.
        lines = [f"- {c.short_code or c.code}: {c.nickname or c.title or c.name}" for c in courses]
        prompt += "\nTheir courses this term:\n" + "\n".join(lines) + "\n"
    else:
        prompt += "\nNo courses are being tracked yet.\n"

    return prompt
