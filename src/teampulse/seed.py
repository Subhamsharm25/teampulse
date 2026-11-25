"""Demo data: a 10-person startup with four weeks of realistic history (deterministic).

Patterns on purpose, so the dashboard has something to show: a reliable engineer, one who
regularly works late, a part-timer, someone often absent, someone who leaves early, leave
days, a company holiday, and "today" with people clocked in, clocked out and not in yet.
Demo accounts only: the password is shared and documented in the README.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from .db import Database
from .security import hash_password
from .timecalc import Policy

DEMO_PASSWORD = "teampulse-demo"

PEOPLE = [
    # name, email, role, team, expected h/day, profile
    ("Ritu Kapoor", "ritu@demo.teampulse.dev", "admin", "Leadership", 8, "steady"),
    ("Meera Iyer", "meera@demo.teampulse.dev", "manager", "Operations", 8, "steady"),
    ("Priya Nair", "priya@demo.teampulse.dev", "employee", "Engineering", 8, "steady"),
    ("Arjun Mehta", "arjun@demo.teampulse.dev", "employee", "Engineering", 8, "overtime"),
    ("Ananya Rao", "ananya@demo.teampulse.dev", "employee", "Engineering", 8, "steady"),
    ("Kabir Singh", "kabir@demo.teampulse.dev", "employee", "Design", 8, "late"),
    ("Neha Gupta", "neha@demo.teampulse.dev", "employee", "Design", 8, "steady"),
    ("Rahul Verma", "rahul@demo.teampulse.dev", "employee", "Sales", 8, "absentee"),
    ("Sara Khan", "sara@demo.teampulse.dev", "employee", "Sales", 4, "parttime"),
    ("Vikram Joshi", "vikram@demo.teampulse.dev", "employee", "Operations", 8, "short"),
]


def _day_plan(profile: str, rng: random.Random) -> tuple[float, float] | None:
    """(start hour, hours worked) for one work day, or None for absent."""
    jitter = rng.uniform(-0.3, 0.4)
    if profile == "absentee" and rng.random() < 0.25:
        return None
    if rng.random() < 0.03:
        return None  # anyone can miss a day
    start = {"late": 10.6, "parttime": 13.5}.get(profile, 9.4) + rng.uniform(-0.3, 0.5)
    hours = {"steady": 8.3, "overtime": 10.2, "late": 7.4, "absentee": 6.4, "parttime": 4.1, "short": 5.6}[profile]
    return start, max(hours + jitter, 1.0)


def _sessions_for(day: date, start_h: float, hours: float, tz) -> list[tuple[datetime, datetime]]:
    start = datetime.combine(day, time(0), tzinfo=tz) + timedelta(hours=start_h)
    if hours <= 5:
        return [(start, start + timedelta(hours=hours))]
    morning = min(4.0, hours / 2)
    lunch = timedelta(minutes=45)
    first_end = start + timedelta(hours=morning)
    return [(start, first_end), (first_end + lunch, first_end + lunch + timedelta(hours=hours - morning))]


def seed(db: Database, policy: Policy, now: datetime, weeks: int = 4, seed_value: int = 7) -> dict[str, int]:
    if db.employees():
        raise ValueError("database already has people; seed only an empty database")
    rng = random.Random(seed_value)
    tz = policy.tz
    today = now.astimezone(tz).date()
    pw = hash_password(DEMO_PASSWORD)
    ids = {p[1]: db.add_employee(p[0], p[1], p[2], p[3], p[4], pw) for p in PEOPLE}

    first = today - timedelta(days=weeks * 7)
    holiday = first + timedelta(days=(7 - first.weekday()) % 7 + 9)  # a weekday about 1.5 weeks in
    while holiday.weekday() not in policy.workdays:
        holiday += timedelta(days=1)
    db.add_leave(None, holiday, "holiday", "Company offsite day")
    db.add_leave(ids["neha@demo.teampulse.dev"], today - timedelta(days=8), "leave", "Family function")
    db.add_leave(ids["neha@demo.teampulse.dev"], today - timedelta(days=9), "leave", "Family function")
    db.add_leave(ids["ananya@demo.teampulse.dev"], today - timedelta(days=4), "sick", "Fever")

    leaves = db.leave_days(first, today)
    count = 0
    day = first
    while day < today:
        if day.weekday() in policy.workdays and day != holiday:
            for _name, email, _, _, _, profile in PEOPLE:
                if day in leaves.get(ids[email], set()):
                    continue
                plan = _day_plan(profile, rng)
                if plan:
                    for s, e in _sessions_for(day, plan[0], plan[1], tz):
                        db.add_manual_session(ids[email], s, e, "")
                        count += 1
        day += timedelta(days=1)

    # Today: a realistic snapshot relative to the current time.
    if today.weekday() in policy.workdays and today != holiday:
        local_now = now.astimezone(tz)
        for _, email, _, _, _, profile in PEOPLE:
            if profile == "absentee" or today in leaves.get(ids[email], set()):
                continue  # not in yet / on leave
            plan = _day_plan(profile, rng) or (9.5, 8.0)
            sessions = _sessions_for(today, plan[0], plan[1], tz)
            for i, (s, e) in enumerate(sessions):
                if s >= local_now:
                    break
                if e <= local_now:
                    db.add_manual_session(ids[email], s, e, "")
                elif i == len(sessions) - 1 or profile != "short":
                    db.clock_in(ids[email], s)  # still working right now
                    break
                count += 1
    return {"employees": len(ids), "sessions": count}
