"""SQLite storage (standard library sqlite3). One file, no server: fits a small startup."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from .timecalc import Session

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'employee')),
    team          TEXT NOT NULL DEFAULT 'General',
    expected_hours REAL NOT NULL DEFAULT 8 CHECK (expected_hours >= 0 AND expected_hours <= 24),
    active        INTEGER NOT NULL DEFAULT 1,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    start_utc   TEXT NOT NULL,
    end_utc     TEXT,
    note        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'clock' CHECK (source IN ('clock', 'manual'))
);
CREATE INDEX IF NOT EXISTS ix_sessions_emp_start ON sessions(employee_id, start_utc);
-- at most one open session per employee
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_open ON sessions(employee_id) WHERE end_utc IS NULL;
CREATE TABLE IF NOT EXISTS leaves (
    id          INTEGER PRIMARY KEY,
    employee_id INTEGER REFERENCES employees(id) ON DELETE CASCADE,  -- NULL = company holiday
    day         TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('leave', 'sick', 'holiday')),
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE (employee_id, day)
);
"""


def iso(dt: datetime) -> str:
    """Canonical stored form: UTC, whole seconds, so string order equals time order in SQL."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if self.path == ":memory:" else None
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._mem or sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if self._mem is None:
                conn.close()

    # ------------------------------------------------------------ employees
    def add_employee(
        self, name: str, email: str, role: str, team: str, expected_hours: float, password_hash: str
    ) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO employees (name, email, role, team, expected_hours, password_hash) VALUES (?,?,?,?,?,?)",
                (name.strip(), email.strip().lower(), role, team.strip() or "General", expected_hours, password_hash),
            )
            return int(cur.lastrowid)

    def employees(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM employees" + (" WHERE active = 1" if active_only else "") + " ORDER BY team, name"
        with self.connect() as c:
            return c.execute(sql).fetchall()

    def employee(self, employee_id: int) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM employees WHERE id = ?", (employee_id,)).fetchone()

    def employee_by_email(self, email: str) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute("SELECT * FROM employees WHERE email = ?", (email.strip().lower(),)).fetchone()

    def set_active(self, employee_id: int, active: bool) -> None:
        with self.connect() as c:
            c.execute("UPDATE employees SET active = ? WHERE id = ?", (int(active), employee_id))
            if not active:  # close a dangling session when someone is deactivated
                c.execute(
                    "UPDATE sessions SET end_utc = strftime('%Y-%m-%dT%H:%M:%S+00:00','now') "
                    "WHERE employee_id = ? AND end_utc IS NULL",
                    (employee_id,),
                )

    # ------------------------------------------------------------- sessions
    @staticmethod
    def _session(row: sqlite3.Row) -> Session:
        return Session(
            employee_id=row["employee_id"],
            start=datetime.fromisoformat(row["start_utc"]),
            end=datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
            id=row["id"],
            note=row["note"],
        )

    def sessions_between(self, lo: datetime, hi: datetime, employee_id: int | None = None) -> list[Session]:
        """Sessions overlapping [lo, hi) (open sessions included)."""
        sql = "SELECT * FROM sessions WHERE start_utc < ? AND (end_utc IS NULL OR end_utc > ?)"
        args: list = [iso(hi), iso(lo)]
        if employee_id is not None:
            sql += " AND employee_id = ?"
            args.append(employee_id)
        with self.connect() as c:
            return [self._session(r) for r in c.execute(sql + " ORDER BY start_utc", args)]

    def open_session(self, employee_id: int) -> Session | None:
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM sessions WHERE employee_id = ? AND end_utc IS NULL", (employee_id,)
            ).fetchone()
        return self._session(row) if row else None

    def clock_in(self, employee_id: int, at: datetime) -> int:
        with self.connect() as c:
            if c.execute("SELECT 1 FROM sessions WHERE employee_id=? AND end_utc IS NULL", (employee_id,)).fetchone():
                raise ValueError("already clocked in")
            cur = c.execute("INSERT INTO sessions (employee_id, start_utc) VALUES (?, ?)", (employee_id, iso(at)))
            return int(cur.lastrowid)

    def clock_out(self, employee_id: int, at: datetime) -> None:
        with self.connect() as c:
            cur = c.execute(
                "UPDATE sessions SET end_utc = ? WHERE employee_id = ? AND end_utc IS NULL AND start_utc < ?",
                (iso(at), employee_id, iso(at)),
            )
            if cur.rowcount == 0:
                raise ValueError("not clocked in")

    def add_manual_session(self, employee_id: int, start: datetime, end: datetime, note: str) -> int:
        if end <= start:
            raise ValueError("end must be after start")
        if (end - start).total_seconds() > 16 * 3600:
            raise ValueError("a single entry cannot be longer than 16 hours")
        if self.sessions_between(start, end, employee_id):
            raise ValueError("overlaps an existing entry")
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO sessions (employee_id, start_utc, end_utc, note, source) VALUES (?,?,?,?, 'manual')",
                (employee_id, iso(start), iso(end), note.strip()),
            )
            return int(cur.lastrowid)

    def delete_session(self, session_id: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # --------------------------------------------------------------- leaves
    def add_leave(self, employee_id: int | None, day: date, kind: str, note: str = "") -> None:
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO leaves (employee_id, day, kind, note) VALUES (?,?,?,?)",
                (employee_id, day.isoformat(), kind, note.strip()),
            )

    def leave_days(self, lo: date, hi: date) -> dict[int | None, set[date]]:
        """{employee_id: {days}}; key None holds company holidays."""
        out: dict[int | None, set[date]] = {}
        with self.connect() as c:
            for r in c.execute(
                "SELECT employee_id, day FROM leaves WHERE day BETWEEN ? AND ?", (lo.isoformat(), hi.isoformat())
            ):
                out.setdefault(r["employee_id"], set()).add(date.fromisoformat(r["day"]))
        return out

    def leaves_for(self, employee_id: int, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM leaves WHERE employee_id = ? OR employee_id IS NULL ORDER BY day DESC LIMIT ?",
                (employee_id, limit),
            ).fetchall()
