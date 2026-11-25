"""Reports that combine stored data with the hours engine: daily dashboard, trend, period report, CSV."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import Database
from .timecalc import DaySummary, PeriodSummary, Policy, day_bounds, summarize_day, summarize_period


@dataclass
class Row:
    employee: dict
    today: DaySummary


@dataclass
class Dashboard:
    day: date
    rows: list[Row]

    def _days(self) -> list[DaySummary]:
        return [r.today for r in self.rows]

    @property
    def headcount(self) -> int:
        return len(self.rows)

    @property
    def working_now(self) -> int:
        return sum(d.working_now for d in self._days())

    @property
    def present(self) -> int:
        return sum(1 for d in self._days() if d.worked_h > 0)

    @property
    def absent(self) -> int:
        return sum(1 for d in self._days() if d.status in ("absent", "not_in"))

    @property
    def on_leave(self) -> int:
        return sum(1 for d in self._days() if d.status == "leave")

    @property
    def worked_h(self) -> float:
        return round(sum(d.worked_h for d in self._days()), 2)

    @property
    def expected_h(self) -> float:
        return round(sum(d.expected_h for d in self._days()), 2)

    @property
    def not_worked_h(self) -> float:
        return round(sum(d.not_worked_h for d in self._days()), 2)

    @property
    def utilization(self) -> float | None:
        return round(self.worked_h / self.expected_h, 3) if self.expected_h else None

    def as_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "headcount": self.headcount,
            "working_now": self.working_now,
            "present": self.present,
            "absent": self.absent,
            "on_leave": self.on_leave,
            "worked_h": self.worked_h,
            "expected_h": self.expected_h,
            "not_worked_h": self.not_worked_h,
            "utilization": self.utilization,
            "employees": [
                {
                    "id": r.employee["id"],
                    "name": r.employee["name"],
                    "team": r.employee["team"],
                    "status": r.today.status,
                    "working_now": r.today.working_now,
                    "worked_h": r.today.worked_h,
                    "expected_h": r.today.expected_h,
                    "not_worked_h": r.today.not_worked_h,
                    "overtime_h": r.today.overtime_h,
                    "utilization": r.today.utilization,
                    "first_in": r.today.first_in.isoformat() if r.today.first_in else None,
                    "last_out": r.today.last_out.isoformat() if r.today.last_out else None,
                }
                for r in self.rows
            ],
        }


class Reports:
    def __init__(self, db: Database, policy: Policy) -> None:
        self.db, self.policy = db, policy

    def _leaves(self, lo: date, hi: date) -> tuple[dict, set[date]]:
        leaves = self.db.leave_days(lo, hi)
        return leaves, leaves.get(None, set())

    def dashboard(self, day: date, now: datetime, team: str | None = None) -> Dashboard:
        lo, hi = day_bounds(day, self.policy.tz)
        sessions = self.db.sessions_between(lo, hi)
        leaves, holidays = self._leaves(day, day)
        rows = []
        for e in self.db.employees(active_only=True):
            if team and e["team"] != team:
                continue
            mine = leaves.get(e["id"], set())
            rows.append(
                Row(
                    dict(e),
                    summarize_day(e["id"], e["expected_hours"], sessions, day, self.policy, now, mine, holidays),
                )
            )
        order = {
            "working": 0,
            "overtime": 1,
            "on_target": 2,
            "extra": 3,
            "under": 4,
            "not_in": 5,
            "absent": 6,
            "leave": 7,
        }
        rows.sort(key=lambda r: (order.get(r.today.status, 9), r.employee["name"]))
        return Dashboard(day, rows)

    def period(self, start: date, end: date, now: datetime) -> list[tuple[dict, PeriodSummary]]:
        lo, _ = day_bounds(start, self.policy.tz)
        _, hi = day_bounds(end, self.policy.tz)
        sessions = self.db.sessions_between(lo, hi)
        leaves, holidays = self._leaves(start, end)
        out = []
        for e in self.db.employees(active_only=True):
            own_leave = leaves.get(e["id"], set())
            mine = [s for s in sessions if s.employee_id == e["id"]]
            p = summarize_period(e["id"], e["expected_hours"], mine, start, end, self.policy, now, own_leave, holidays)
            out.append((dict(e), p))
        return out

    def trend(self, days: int, now: datetime) -> list[dict]:
        """Team worked vs expected hours per day, oldest first (for the dashboard chart)."""
        end = now.astimezone(self.policy.tz).date()
        start = end - timedelta(days=days - 1)
        per_day: dict[date, dict] = {}
        for _, p in self.period(start, end, now):
            for d in p.days:
                agg = per_day.setdefault(
                    d.day, {"day": d.day.isoformat(), "worked_h": 0.0, "expected_h": 0.0, "absent": 0}
                )
                agg["worked_h"] = round(agg["worked_h"] + d.worked_h, 2)
                agg["expected_h"] = round(agg["expected_h"] + d.expected_h, 2)
                agg["absent"] += d.status == "absent"
        return [per_day[k] for k in sorted(per_day)]

    def period_csv(self, start: date, end: date, now: datetime) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(
            [
                "employee",
                "email",
                "team",
                "worked_h",
                "expected_h",
                "not_worked_h",
                "overtime_h",
                "utilization_pct",
                "days_present",
                "days_absent",
                "days_leave",
            ]
        )
        for e, p in self.period(start, end, now):
            util = "" if p.utilization is None else round(p.utilization * 100, 1)
            w.writerow(
                [
                    e["name"],
                    e["email"],
                    e["team"],
                    p.worked_h,
                    p.expected_h,
                    p.not_worked_h,
                    p.overtime_h,
                    util,
                    p.days_present,
                    p.days_absent,
                    p.days_leave,
                ]
            )
        return buf.getvalue()
