from datetime import UTC, date, datetime, timedelta

import pytest

from teampulse.db import Database, iso
from teampulse.security import LoginLimiter, hash_password, verify_password

T0 = datetime(2026, 11, 25, 4, 0, tzinfo=UTC)


@pytest.fixture
def db():
    d = Database(":memory:")
    d.add_employee("Asha", "asha@x.dev", "employee", "Eng", 8, "x")
    return d


def test_iso_is_canonical_utc():
    from zoneinfo import ZoneInfo

    t = datetime(2026, 11, 25, 9, 30, 15, 123456, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert iso(t) == "2026-11-25T04:00:15+00:00"
    with pytest.raises(ValueError):
        iso(datetime(2026, 1, 1))


def test_clock_in_out_rules(db):
    db.clock_in(1, T0)
    with pytest.raises(ValueError, match="already"):
        db.clock_in(1, T0 + timedelta(hours=1))
    assert db.open_session(1).start == T0
    db.clock_out(1, T0 + timedelta(hours=8))
    with pytest.raises(ValueError, match="not clocked in"):
        db.clock_out(1, T0 + timedelta(hours=9))
    (only,) = db.sessions_between(T0, T0 + timedelta(days=1), 1)
    assert only.end - only.start == timedelta(hours=8)


def test_manual_entries_are_validated(db):
    db.add_manual_session(1, T0, T0 + timedelta(hours=4), "forgot")
    with pytest.raises(ValueError, match="overlaps"):
        db.add_manual_session(1, T0 + timedelta(hours=3), T0 + timedelta(hours=5), "")
    with pytest.raises(ValueError, match="after start"):
        db.add_manual_session(1, T0, T0, "")
    with pytest.raises(ValueError, match="16 hours"):
        db.add_manual_session(1, T0 + timedelta(days=2), T0 + timedelta(days=3), "")


def test_sessions_between_includes_open_and_boundary_crossing(db):
    db.add_manual_session(1, T0 - timedelta(hours=2), T0 + timedelta(hours=1), "")
    db.clock_in(1, T0 + timedelta(hours=5))
    found = db.sessions_between(T0, T0 + timedelta(hours=6))
    assert len(found) == 2 and found[1].end is None


def test_leaves_and_holidays(db):
    db.add_leave(1, date(2026, 11, 20), "sick")
    db.add_leave(None, date(2026, 11, 21), "holiday", "Offsite")
    db.add_leave(1, date(2026, 11, 20), "leave")  # replaces, not duplicates
    leaves = db.leave_days(date(2026, 11, 1), date(2026, 11, 30))
    assert leaves == {1: {date(2026, 11, 20)}, None: {date(2026, 11, 21)}}
    assert [r["kind"] for r in db.leaves_for(1)] == ["holiday", "leave"]


def test_deactivating_closes_open_session(db):
    db.clock_in(1, T0)
    db.set_active(1, False)
    assert db.open_session(1) is None and not db.employee(1)["active"]


def test_password_hashing_and_limiter():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) and not verify_password("wrong", h)
    assert not verify_password("x", "garbage")
    with pytest.raises(ValueError):
        hash_password("short")
    clock = [0.0]
    lim = LoginLimiter(attempts=2, window_s=60, clock=lambda: clock[0])
    lim.fail("A@x.dev")
    lim.fail("a@x.dev")
    assert lim.blocked("a@x.dev")
    clock[0] = 61
    assert not lim.blocked("a@x.dev")
