"""Notification slots: which scheduled messages are due right now.

Pure functions, no I/O. The cron endpoint calls this every ~15 minutes with the current
time in the user's timezone. A slot is due from its start until start + grace, so a late
or missed ping self-heals without ever sending a stale message hours later. Per-channel
de-duplication is the `digests` table (unique on local_date, channel, kind).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

ROLES = ("digest", "nudge", "review", "checkin")
_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Slot:
    role: str
    weekdays: frozenset[int]
    hour: int
    minute: int

    @property
    def kind(self) -> str:
        """Stored in digests.kind (String(32))."""
        return self.role


def _parse_days(token: str) -> frozenset[int]:
    token = token.strip().lower()
    if token == "daily":
        return frozenset(range(7))
    days: set[int] = set()
    for part in token.split("+"):
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in _DAYS or b not in _DAYS:
                raise ValueError(f"Bad day range {part!r}")
            i, j = _DAYS[a], _DAYS[b]
            days.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
        elif part in _DAYS:
            days.add(_DAYS[part])
        else:
            raise ValueError(f"Bad day {part!r}; use daily, sat, mon-fri, sat+sun")
    return frozenset(days)


def parse_slot(role: str, spec: str) -> Slot:
    """'daily@07:00' | 'sat@08:00' | 'mon-fri@07:30' -> Slot. Raises ValueError."""
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}")
    days_part, sep, time_part = spec.strip().partition("@")
    m = _TIME_RE.match(time_part) if sep else None
    if not m:
        raise ValueError(f"Bad slot {spec!r} for {role}; expected e.g. 'daily@07:00'")
    return Slot(role, _parse_days(days_part), int(m.group(1)), int(m.group(2)))


def due_slots(
    now_local: datetime,
    slots: list[Slot],
    grace: timedelta,
    *,
    review_replaces_daily: bool = True,
) -> list[Slot]:
    """Slots whose window [start, start + grace) contains now_local (tz-aware).

    On a day that has a `review` slot, the daily `digest` and `nudge` are dropped: the
    weekly review is a superset of the digest, and the `checkin` covers the nudge's job.
    """
    wd = now_local.weekday()
    due = []
    for s in slots:
        if wd not in s.weekdays:
            continue
        start = now_local.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
        if start <= now_local < start + grace:
            due.append(s)
    if review_replaces_daily and any(s.role == "review" and wd in s.weekdays for s in slots):
        due = [s for s in due if s.role not in ("digest", "nudge")]
    return due
