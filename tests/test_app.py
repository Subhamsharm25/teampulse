import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from teampulse.app import create_app
from teampulse.config import Settings
from teampulse.db import Database
from teampulse.seed import DEMO_PASSWORD, seed

NOW = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)  # Wednesday 13:30 in Asia/Kolkata


@pytest.fixture
def settings():
    return Settings(
        db_path=":memory:",
        tz="Asia/Kolkata",
        workdays=frozenset(range(5)),
        under_ratio=0.75,
        secret_key="test-secret",
        secure_cookies=False,
        company="Acme Labs",
    )


@pytest.fixture
def db(settings):
    d = Database(":memory:")
    seed(d, settings.policy, NOW)
    return d


@pytest.fixture
def app(settings, db):
    clock = {"now": NOW}
    a = create_app(settings, db, clock=lambda: clock["now"])
    a.state.test_clock = clock
    return a


def client(app) -> TestClient:
    return TestClient(app)


def csrf(c: TestClient, path: str = "/login") -> str:
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def login(app, email: str, password: str = DEMO_PASSWORD) -> TestClient:
    c = client(app)
    r = c.post("/login", data={"email": email, "password": password, "csrf": csrf(c)})
    assert r.status_code == 200, r.text
    return c


ADMIN, MANAGER, EMPLOYEE = "ritu@demo.teampulse.dev", "meera@demo.teampulse.dev", "priya@demo.teampulse.dev"


def test_redirects_to_login_and_rejects_bad_password(app):
    c = client(app)
    assert c.get("/").url.path == "/login"
    assert c.get("/api/v1/dashboard").status_code == 401
    r = c.post("/login", data={"email": ADMIN, "password": "nope-nope", "csrf": csrf(c)})
    assert "Wrong email or password" in r.text


def test_login_is_rate_limited(app):
    c = client(app)
    for _ in range(5):
        c.post("/login", data={"email": ADMIN, "password": "nope-nope", "csrf": csrf(c)})
    r = c.post("/login", data={"email": ADMIN, "password": DEMO_PASSWORD, "csrf": csrf(c)})
    assert "Too many attempts" in r.text


def test_forms_need_csrf_token(app):
    c = client(app)
    r = c.post("/login", data={"email": ADMIN, "password": DEMO_PASSWORD, "csrf": "forged"})
    assert r.status_code == 403


def test_manager_dashboard_and_api(app):
    c = login(app, MANAGER)
    page = c.get("/")
    assert page.status_code == 200 and "Working now" in page.text and "Hours not worked" in page.text
    data = c.get("/api/v1/dashboard").json()
    assert data["headcount"] == 10 and data["day"] == "2026-11-25"
    assert data["working_now"] >= 1 and data["absent"] >= 1  # seed: Rahul never in today
    assert data["worked_h"] + data["not_worked_h"] >= data["expected_h"] - 0.01
    trend = c.get("/api/v1/trend?days=14").json()["days"]
    assert len(trend) == 14 and trend[-1]["day"] == "2026-11-25"
    assert c.get("/api/v1/dashboard?day=not-a-date").status_code == 400


def test_employee_sees_only_own_page(app):
    c = login(app, EMPLOYEE)
    assert c.get("/").url.path == "/me"
    assert c.get("/reports").status_code == 403
    assert c.get("/api/v1/dashboard").status_code == 403
    assert c.get("/api/v1/me").json()["name"] == "Priya Nair"


def test_clock_in_and_out(app):
    c = login(app, "rahul@demo.teampulse.dev")  # not clocked in today in the seed
    token = csrf(c, "/me")
    r = c.post("/me/clock", data={"csrf": token})
    assert "Clocked in" in r.text
    app.state.test_clock["now"] = NOW + timedelta(hours=3)
    r = c.post("/me/clock", data={"csrf": token})
    assert "Clocked out" in r.text
    me = c.get("/api/v1/me").json()["today"]
    assert me["worked_h"] == 3.0 and me["status"] == "under"


def test_manager_fixes_time_and_records_leave(app, db):
    c = login(app, MANAGER)
    rahul = db.employee_by_email("rahul@demo.teampulse.dev")["id"]
    token = csrf(c, f"/employees/{rahul}")
    r = c.post(
        f"/employees/{rahul}/sessions",
        data={"day": "2026-11-25", "start": "09:30", "end": "12:30", "note": "forgot", "csrf": token},
    )
    assert "Time entry added" in r.text
    r = c.post(
        f"/employees/{rahul}/sessions", data={"day": "2026-11-25", "start": "13:00", "end": "18:00", "csrf": token}
    )
    assert "cannot end in the future" in r.text
    r = c.post(f"/employees/{rahul}/leave", data={"day": "2026-11-26", "kind": "sick", "csrf": token})
    assert "Leave recorded" in r.text


def test_admin_manages_people_but_manager_cannot(app):
    m = login(app, MANAGER)
    r = m.post(
        "/employees", data={"name": "X", "email": "x@y.dev", "password": "longenough", "csrf": csrf(m, "/employees")}
    )
    assert r.status_code == 403
    a = login(app, ADMIN)
    token = csrf(a, "/employees")
    data = {
        "name": "Zoya Ali",
        "email": "zoya@demo.teampulse.dev",
        "team": "Eng",
        "role": "employee",
        "expected_hours": "6",
        "password": "welcome-zoya",
        "csrf": token,
    }
    assert "Added Zoya Ali" in a.post("/employees", data=data).text
    assert "already registered" in a.post("/employees", data=data).text
    assert login(app, "zoya@demo.teampulse.dev", "welcome-zoya").get("/me").status_code == 200


def test_reports_and_csv_do_not_count_holidays_as_leave(app):
    c = login(app, MANAGER)
    page = c.get("/reports?start=2026-10-28&end=2026-11-24")
    assert page.status_code == 200 and "Download CSV" in page.text
    csv_text = c.get("/reports.csv?start=2026-10-28&end=2026-11-24").text
    header, *rows = csv_text.strip().splitlines()
    assert header.startswith("employee,email,team,worked_h") and len(rows) == 10
    leave = {r.split(",")[0]: int(r.split(",")[-1]) for r in rows}
    # seed: Neha 2 leave days, Ananya 1 sick day; the company holiday is not anyone's leave
    assert leave["Neha Gupta"] == 2 and leave["Ananya Rao"] == 1 and leave["Priya Nair"] == 0
    assert c.get("/reports?start=2026-11-10&end=2026-11-01").status_code == 400


def test_health(app):
    assert client(app).get("/healthz").text == "ok"
