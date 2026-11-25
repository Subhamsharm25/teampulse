"""Hours engine: turns clock-in/clock-out sessions into daily and period summaries.

Pure functions with no database or web code, so every rule is unit-tested directly.

Rules
* A day is a calendar day in the company time zone. A session crossing midnight is split
  across both days; an open session (still clocked in) counts up to "now".
* Expected hours apply on work days only, and not on leave days or company holidays.
* not_worked = max(expected - worked, 0); overtime = max(worked - expected, 0).
* Status of a past day: leave, holiday, off (non-work day), extra (worked when nothing was
  expected; it all counts as overtime), absent (0 h on a work day),
  under (< under_ratio of expected), on_target, overtime (> expected + overtime_margin).
  Today: working (clocked in now), not_in (nothing yet), otherwise the same thresholds
  on the hours so far (so someone who clocked out early shows as under target).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Session:
    employee_id: int
    start: datetime  # timezone-aware
    end: datetime | None  # None while clocked in
    id: int = 0
    note: str = ""


@dataclass(frozen=True)
class Policy:
    tz: ZoneInfo
    workdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})  # Monday=0
    under_ratio: float = 0.75
    overtime_margin_h: float = 1.0


@dataclass
class DaySummary:
    employee_id: int
    day: date
    worked_h: float
    expected_h: float
    status: str
    working_now: bool = False
    first_in: datetime | None = None
    last_out: datetime | None = None

    @property
    def not_worked_h(self) -> float:
        return round(max(self.expected_h - self.worked_h, 0.0), 2)

    @property
    def overtime_h(self) -> float:
        return round(max(self.worked_h - self.expected_h, 0.0), 2)

    @property
    def utilization(self) -> float | None:
        return round(self.worked_h / self.expected_h, 3) if self.expected_h else None


@dataclass
class PeriodSummary:
    employee_id: int
    days: list[DaySummary] = field(default_factory=list)

    def _sum(self, attr: str) -> float:
        return round(sum(getattr(d, attr) for d in self.days), 2)

    @property
    def worked_h(self) -> float:
        return self._sum("worked_h")

    @property
    def expected_h(self) -> float:
        return self._sum("expected_h")

    @property
    def not_worked_h(self) -> float:
        return self._sum("not_worked_h")

    @property
    def overtime_h(self) -> float:
        return self._sum("overtime_h")

    @property
    def utilization(self) -> float | None:
        return round(self.worked_h / self.expected_h, 3) if self.expected_h else None

    def count(self, *statuses: str) -> int:
        return sum(1 for d in self.days if d.status in statuses)

    @property
    def days_present(self) -> int:
        return sum(1 for d in self.days if d.worked_h > 0)

    @property
    def days_absent(self) -> int:
        return self.count("absent")

    @property
    def days_leave(self) -> int:
        return self.count("leave")


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local midnight to midnight, returned in UTC.

    Python subtracts two datetimes that share a tzinfo by wall-clock time, so local bounds would
    make a daylight-saving day 24 h long instead of 23 or 25. UTC arithmetic is always exact.
    """
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    return start, end


def overlap_seconds(s: Session, lo: datetime, hi: datetime, now: datetime) -> float:
    start, end = s.start.astimezone(UTC), (s.end or now).astimezone(UTC)
    return max((min(end, hi) - max(start, lo)).total_seconds(), 0.0)


def summarize_day(
    employee_id: int,
    expected_per_day: float,
    sessions: Iterable[Session],
    day: date,
    policy: Policy,
    now: datetime,
    leave_days: set[date] | frozenset[date] = frozenset(),
    holidays: set[date] | frozenset[date] = frozenset(),
) -> DaySummary:
    lo, hi = day_bounds(day, policy.tz)
    mine = [s for s in sessions if s.employee_id == employee_id and overlap_seconds(s, lo, hi, now) > 0]
    worked = round(sum(overlap_seconds(s, lo, hi, now) for s in mine) / 3600, 2)
    today = now.astimezone(policy.tz).date()
    workday = day.weekday() in policy.workdays
    on_leave = day in leave_days
    holiday = day in holidays
    expected = expected_per_day if workday and not on_leave and not holiday else 0.0
    working_now = day == today and any(s.end is None and s.start <= now for s in mine)
    first_in = min((s.start for s in mine), default=None)
    closed = [s.end for s in mine if s.end is not None]
    last_out = max(closed, default=None) if not working_now else None

    if day > today:
        status = "upcoming"
    elif day == today and working_now:
        status = "working"
    elif expected == 0:
        # Nothing was expected: time worked is extra (it all counts as overtime).
        status = "extra" if worked > 0 else "holiday" if holiday else "leave" if on_leave else "off"
    elif worked == 0:
        status = "not_in" if day == today else "absent"
    elif expected and worked > expected + policy.overtime_margin_h:
        status = "overtime"
    elif expected and worked < expected * policy.under_ratio:
        status = "under"  # today this means "clocked out early"; while clocked in it is "working"
    else:
        status = "on_target"
    return DaySummary(employee_id, day, worked, expected, status, working_now, first_in, last_out)


def summarize_period(
    employee_id: int,
    expected_per_day: float,
    sessions: list[Session],
    start: date,
    end: date,
    policy: Policy,
    now: datetime,
    leave_days: set[date] | frozenset[date] = frozenset(),
    holidays: set[date] | frozenset[date] = frozenset(),
) -> PeriodSummary:
    if end < start:
        raise ValueError("end date is before start date")
    out = PeriodSummary(employee_id)
    day = start
    while day <= end:
        out.days.append(summarize_day(employee_id, expected_per_day, sessions, day, policy, now, leave_days, holidays))
        day += timedelta(days=1)
    return out


def utc_now() -> datetime:
    return datetime.now(UTC)
