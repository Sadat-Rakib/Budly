from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from canvasbuddy.slots import due_slots, parse_slot

TZ = ZoneInfo("America/Edmonton")
G = timedelta(minutes=180)
DIGEST = parse_slot("digest", "daily@07:00")
NUDGE = parse_slot("nudge", "daily@20:00")
REVIEW = parse_slot("review", "sat@08:00")
CHECKIN = parse_slot("checkin", "sat@15:00")
ALL = [DIGEST, NUDGE, REVIEW, CHECKIN]


def at(m, d, h, mi=0, y=2026):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def roles(dt):
    return [s.role for s in due_slots(dt, ALL, G)]


def test_parse_variants():
    assert parse_slot("digest", "daily@07:00").weekdays == frozenset(range(7))
    assert parse_slot("digest", "mon-fri@07:30").weekdays == frozenset(range(5))
    assert parse_slot("review", "sat+sun@09:00").weekdays == frozenset({5, 6})
    assert parse_slot("review", "fri-mon@09:00").weekdays == frozenset({4, 5, 6, 0})
    assert REVIEW.kind == "review"


@pytest.mark.parametrize(
    "role,spec",
    [
        ("digest", ""),
        ("digest", "daily@7:00"),
        ("digest", "someday@07:00"),
        ("digest", "sat@25:00"),
        ("bogus", "daily@07:00"),
        ("digest", "daily"),
    ],
)
def test_parse_rejects(role, spec):
    with pytest.raises(ValueError):
        parse_slot(role, spec)


def test_calendar_assumption():
    assert at(9, 26, 8).weekday() == 5  # Saturday
    assert at(9, 23, 8).weekday() == 2  # Wednesday


def test_weekday_morning_and_evening():
    assert roles(at(9, 23, 7, 0)) == ["digest"]
    assert roles(at(9, 23, 20, 15)) == ["nudge"]
    assert roles(at(9, 23, 6, 59)) == []
    assert roles(at(9, 23, 12, 0)) == []


def test_saturday_review_replaces_digest():
    assert roles(at(9, 26, 7, 30)) == []
    assert roles(at(9, 26, 8, 0)) == ["review"]
    assert roles(at(9, 26, 10, 45)) == ["review"]  # late ping still inside grace
    assert roles(at(9, 26, 11, 0)) == []  # stale -> skipped


def test_saturday_checkin_and_no_nudge():
    assert roles(at(9, 26, 15, 15)) == ["checkin"]
    assert roles(at(9, 26, 20, 15)) == []


def test_sunday_back_to_daily():
    assert roles(at(9, 27, 7, 0)) == ["digest"]
    assert roles(at(9, 27, 20, 0)) == ["nudge"]


def test_toggle_off_replacement():
    got = [s.role for s in due_slots(at(9, 26, 7, 30), ALL, G, review_replaces_daily=False)]
    assert got == ["digest"]


def test_dst_uses_local_wall_clock():
    u = ZoneInfo("UTC")  # Alberta falls back Sun 2026-11-01

    def r(y, m, d, h):
        return [s.role for s in due_slots(datetime(y, m, d, h, 0, tzinfo=u).astimezone(TZ), ALL, G)]

    assert r(2026, 10, 31, 14) == ["review"]  # Sat 08:00 MDT
    assert r(2026, 11, 7, 15) == ["review"]  # Sat 08:00 MST
    assert r(2026, 11, 1, 14) == ["digest"]  # Sun 07:00 MST
