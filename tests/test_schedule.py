"""When a tick decides to send.

The cron fires on UTC every 15 minutes; the decision is made in the user's own
timezone. That split is what keeps the schedule correct across daylight saving and
what lets a missed run recover on its own.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from canvasbuddy.cli import should_send_digest

TORONTO = ZoneInfo("America/Toronto")


def at(hour: int, minute: int = 0, *, month: int = 9, day: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TORONTO)


class TestShouldSend:
    def test_not_yet_seven(self) -> None:
        assert not should_send_digest(at(6, 45), 7, already_sent_today=False)

    def test_first_tick_after_seven(self) -> None:
        assert should_send_digest(at(7, 0), 7, already_sent_today=False)

    def test_a_few_minutes_late_still_sends(self) -> None:
        """Railway does not guarantee cron to the minute; 07:14 is a normal first tick."""
        assert should_send_digest(at(7, 14), 7, already_sent_today=False)

    def test_never_twice_in_one_day(self) -> None:
        assert not should_send_digest(at(7, 15), 7, already_sent_today=True)
        assert not should_send_digest(at(22, 0), 7, already_sent_today=True)

    def test_a_missed_morning_self_heals(self) -> None:
        """If the service was down at 07:00, the digest goes out late rather than never.

        A plain UTC cron would simply drop it.
        """
        assert should_send_digest(at(13, 30), 7, already_sent_today=False)

    def test_midnight_starts_a_fresh_day(self) -> None:
        assert not should_send_digest(at(0, 5), 7, already_sent_today=False)

    def test_configurable_hour(self) -> None:
        assert not should_send_digest(at(7, 0), 9, already_sent_today=False)
        assert should_send_digest(at(9, 0), 9, already_sent_today=False)


class TestDaylightSaving:
    def test_the_hour_is_local_on_both_sides_of_the_change(self) -> None:
        """1 November 2026 is the fallback. 07:00 local is 11:00Z before and 12:00Z after.

        Encoding `0 11 * * *` in the crontab would silently fire at 06:00 local for
        four months of the year.
        """
        edt = at(7, 0, month=10, day=30)
        est = at(7, 0, month=11, day=3)

        assert edt.utcoffset().total_seconds() == -4 * 3600
        assert est.utcoffset().total_seconds() == -5 * 3600
        assert should_send_digest(edt, 7, already_sent_today=False)
        assert should_send_digest(est, 7, already_sent_today=False)
