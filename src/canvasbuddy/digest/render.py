"""Telegram MarkdownV2 rendering.

MarkdownV2 is unusually strict: every one of ``_*[]()~`>#+-=|{}.!`` must be
backslash-escaped *anywhere* it appears, including inside ordinary prose. An unescaped
period in a course name or a hyphen in a date does not degrade the formatting -- Telegram
rejects the whole message with a 400. Since almost every line of the digest contains a
course code, a time, or a date, this is the single most likely thing to break a send,
which is why it lives in its own module with its own tests.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

#: Exactly the set Telegram documents for MarkdownV2. The backslash is escaped first.
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_TRANSLATION = str.maketrans({c: "\\" + c for c in "\\" + _MD2_SPECIALS})


def escape_md2(text: str) -> str:
    """Escape text for Telegram MarkdownV2."""
    return text.translate(_TRANSLATION)


def bold(text: str) -> str:
    """Bold an already-escaped string."""
    return f"*{text}*"


def link(text: str, url: str) -> str:
    """A MarkdownV2 inline link.

    Inside the URL only ``)`` and ``\\`` need escaping, and over-escaping there breaks
    the link, so the URL takes a different treatment from the label.
    """
    safe_url = url.replace("\\", "\\\\").replace(")", "\\)")
    return f"[{escape_md2(text)}]({safe_url})"


def local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def format_time(dt: datetime, tz: ZoneInfo) -> str:
    """A time as a person writes it: ``11:59pm``, ``5pm``."""
    moment = local(dt, tz)
    hour = moment.strftime("%I").lstrip("0") or "12"
    suffix = moment.strftime("%p").lower()
    if moment.minute == 0:
        return f"{hour}{suffix}"
    return f"{hour}:{moment.strftime('%M')}{suffix}"


def format_day(dt: datetime, tz: ZoneInfo, *, today: datetime | None = None) -> str:
    """A day label relative to today: ``today``, ``tomorrow``, ``Fri``, ``Nov 15``."""
    moment = local(dt, tz)
    reference = local(today, tz) if today else datetime.now(tz)
    delta = (moment.date() - reference.date()).days

    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 2 <= delta <= 6:
        return moment.strftime("%a")
    return f"{moment.strftime('%b')} {moment.day}"


def format_due(dt: datetime, tz: ZoneInfo, *, today: datetime | None = None) -> str:
    return f"{format_day(dt, tz, today=today)} {format_time(dt, tz)}"


def format_header_date(moment: datetime, tz: ZoneInfo) -> str:
    """``Wednesday, Sep 9`` -- the digest's title line."""
    day = local(moment, tz)
    return f"{day.strftime('%A')}, {day.strftime('%b')} {day.day}"


#: Markdown emphasis a model may add despite being told not to. Only asterisks are
#: stripped: underscores are left alone because tool and field names are full of them
#: ("list_upcoming", "points_possible") and a naive pairwise strip mangles those.
_MD_EMPHASIS = re.compile(r"(\*{1,3})(?=\S)(.+?)(?<=\S)\1", re.S)
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.M)


def strip_markdown(text: str) -> str:
    """Flatten Markdown to plain text for a message sent without a parse mode.

    Telegram renders unparsed Markdown literally, so an unstripped reply arrives full of
    asterisks. Stripping is deliberately preferred over sending with MarkdownV2: escaping
    arbitrary model output is fragile, and a single stray character means Telegram rejects
    the whole message and the user gets no reply at all. Plain text always sends.
    """
    # Bullets first, so a leading "* " is not mistaken for an unclosed emphasis marker.
    text = _MD_BULLET.sub("• ", text)
    text = _MD_HEADING.sub("", text)

    previous = None
    while previous != text:  # nested emphasis, e.g. ***both***
        previous = text
        text = _MD_EMPHASIS.sub(r"\2", text)

    return text.replace("`", "")
