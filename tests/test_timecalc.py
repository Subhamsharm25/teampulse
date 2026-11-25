from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from teampulse.timecalc import Policy, Session, day_bounds, summarize_day, summarize_period

IST = ZoneInfo("Asia/Kolkata")
P = Policy(tz=IST)
WED = date(2026, 11, 25)  # a Wednesday
NOW = datetime(2026, 11, 25, 20, 0, tzinfo=IST)


def s(day: date, start: str, end: str | None, emp: int = 1) -> Session:
    lo = datetime.combine(day, datetime.strptime(start, "%H:%M").time(), tzinfo=IST)
    hi = datetime.combine(day, datetime.strptime(end, "%H:%M").time(), tzinfo=IST) if end else None
    if hi is not None and hi <= lo:
        hi += timedelta(days=1)
    return Session(emp, lo, hi)


def day(sessions, d=WED, now=NOW, leave=frozenset(), hol=frozenset(), expected=8.0):
    return summarize_day(1, expected, sessions, d, P, now, leave, hol)


def test_full_day_with_lunch_break():
    r = day([s(WED, "09:30", "13:30"), s(WED, "14:15", "18:15")])
    assert r.worked_h == 8.0 and r.status == "on_target" and r.not_worked_h == 0 and r.utilization == 1.0
    assert r.first_in.astimezone(IST).strftime("%H:%M") == "09:30"
    assert r.last_out.astimezone(IST).strftime("%H:%M") == "18:15"


def test_dst_day_counts_real_hours():
    berlin = Policy(tz=ZoneInfo("Europe/Berlin"))
    sun = date(2026, 3, 29)  # 02:00 -> 03:00: an hour that does not exist
    night = Session(1, datetime(2026, 3, 29, 0, 0, tzinfo=berlin.tz), datetime(2026, 3, 29, 6, 0, tzinfo=berlin.tz))
    r = summarize_day(1, 0, [night], sun, berlin, datetime(2026, 3, 30, tzinfo=berlin.tz))
    assert r.worked_h == 5.0  # midnight to 06:00 local is only five real hours that night


@pytest.mark.parametrize(
    "start,end,status,not_worked,overtime",
    [
        ("09:00", "18:00", "on_target", 0, 1.0),
        ("09:00", "19:30", "overtime", 0, 2.5),
        ("09:00", "14:00", "under", 3.0, 0),
        ("09:00", "15:00", "on_target", 2.0, 0),
    ],
)
def test_thresholds(start, end, status, not_worked, overtime):
    r = day([s(WED, start, end)], now=NOW + timedelta(days=1))
    assert (r.status, r.not_worked_h, r.overtime_h) == (status, not_worked, overtime)


def test_overnight_shift_is_split_across_days():
    tue = WED - timedelta(days=1)
    shift = [s(tue, "22:00", "06:00")]
    assert day(shift, d=tue).worked_h == 2.0
    assert day(shift, d=WED).worked_h == 6.0


def test_open_session_counts_until_now_and_shows_working():
    r = day([s(WED, "10:00", None)], now=datetime(2026, 11, 25, 13, 30, tzinfo=IST))
    assert r.worked_h == 3.5 and r.status == "working" and r.working_now and r.last_out is None


def test_absent_vs_not_in_yet():
    assert day([], now=datetime(2026, 11, 25, 9, 0, tzinfo=IST)).status == "not_in"
    past = day([], now=NOW + timedelta(days=1))
    assert past.status == "absent" and past.not_worked_h == 8.0


def test_leave_holiday_weekend_and_extra_days():
    assert day([], leave={WED}).status == "leave"
    hol = day([], hol={WED})
    assert hol.status == "holiday" and hol.expected_h == 0 and hol.not_worked_h == 0
    sat = WED + timedelta(days=3)
    assert day([], d=sat, now=NOW + timedelta(days=4)).status == "off"
    extra = day([s(sat, "10:00", "13:00")], d=sat, now=NOW + timedelta(days=4))
    assert extra.status == "extra" and extra.overtime_h == 3.0 and extra.utilization is None


def test_upcoming_days_have_no_judgement():
    assert day([], d=WED + timedelta(days=1)).status == "upcoming"


def test_other_employees_sessions_are_ignored():
    assert day([s(WED, "09:00", "17:00", emp=2)]).worked_h == 0


def test_period_totals():
    mon = WED - timedelta(days=2)
    sessions = [s(mon, "09:00", "17:00"), s(mon + timedelta(days=1), "09:00", "20:00")]
    p = summarize_period(
        1,
        8,
        sessions,
        mon,
        WED + timedelta(days=4),
        P,
        NOW + timedelta(days=7),
        leave_days={WED + timedelta(days=1)},
        holidays={WED + timedelta(days=2)},
    )
    # Mon 8h, Tue 11h, Wed absent, Thu leave, Fri holiday, Sat/Sun off
    assert p.worked_h == 19 and p.expected_h == 24 and p.not_worked_h == 8 and p.overtime_h == 3
    assert (p.days_present, p.days_absent, p.days_leave) == (2, 1, 1)
    assert p.utilization == round(19 / 24, 3)
    with pytest.raises(ValueError):
        summarize_period(1, 8, [], WED, WED - timedelta(days=1), P, NOW)


def test_day_bounds_follow_dst_in_other_time_zones():
    berlin = ZoneInfo("Europe/Berlin")
    lo, hi = day_bounds(date(2026, 3, 29), berlin)  # clocks go forward: a 23-hour day
    assert (hi - lo) == timedelta(hours=23)
