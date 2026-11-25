"""Web app: HTML pages for people, JSON API for integrations and the dashboard chart.

Roles: employee (own clock and hours), manager (dashboard, reports, corrections, leave),
admin (everything, plus adding and deactivating people).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .config import Settings
from .db import Database
from .reports import Reports
from .security import LoginLimiter, csrf_ok, hash_password, new_csrf_token, verify_password
from .timecalc import day_bounds, summarize_period, utc_now

HERE = Path(__file__).parent
ROLES = ("employee", "manager", "admin")
STATUS_LABEL = {
    "working": "Working now",
    "on_target": "On target",
    "overtime": "Overtime",
    "under": "Under target",
    "not_in": "Not in yet",
    "absent": "Absent",
    "leave": "On leave",
    "off": "Day off",
    "holiday": "Holiday",
    "extra": "Extra day",
    "upcoming": "Upcoming",
}


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "–"
    minutes = int(round(h * 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def create_app(settings: Settings, db: Database | None = None, clock: Callable[[], datetime] = utc_now) -> FastAPI:
    db = db or Database(settings.db_path)
    policy = settings.policy
    reports = Reports(db, policy)
    limiter = LoginLimiter()
    app = FastAPI(title="TeamPulse", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.secure_cookies,
        max_age=12 * 3600,
        session_cookie="teampulse",
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters.update(hours=fmt_hours, pct=fmt_pct, status=lambda s: STATUS_LABEL.get(s, s))
    templates.env.globals.update(company=settings.company, version=__version__)
    app.state.db, app.state.reports, app.state.clock = db, reports, clock

    def local(dt: datetime | None) -> str:
        return dt.astimezone(policy.tz).strftime("%H:%M") if dt else "–"

    templates.env.filters["clock"] = local

    def today() -> date:
        return clock().astimezone(policy.tz).date()

    # ------------------------------------------------------------ auth deps
    def current_user(request: Request) -> dict:
        uid = request.session.get("uid")
        user = db.employee(uid) if uid else None
        if not user or not user["active"]:
            request.session.clear()
            raise HTTPException(status_code=401, detail="login required")
        return dict(user)

    def require(role: str):
        def dep(user: dict = Depends(current_user)) -> dict:
            if ROLES.index(user["role"]) < ROLES.index(role):
                raise HTTPException(status_code=403, detail=f"{role} role required")
            return user

        return dep

    def check_csrf(request: Request, csrf: str = Form("")) -> None:
        if not csrf_ok(request.session.get("csrf"), csrf):
            raise HTTPException(status_code=403, detail="invalid form token; reload the page")

    def page(request: Request, name: str, user: dict | None, status_code: int = 200, **ctx) -> HTMLResponse:
        if "csrf" not in request.session:
            request.session["csrf"] = new_csrf_token()
        ctx = {"user": user, "csrf": request.session["csrf"], **ctx}
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    @app.exception_handler(HTTPException)
    async def http_errors(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        return page(request, "error.html", None, status_code=exc.status_code, code=exc.status_code, message=exc.detail)

    def back(url: str, msg: str | None = None, error: str | None = None) -> RedirectResponse:
        return RedirectResponse(
            url + (f"?msg={quote(msg)}" if msg else f"?error={quote(error)}" if error else ""), status_code=303
        )

    def parse_day(value: str | None, default: date) -> date:
        if not value:
            return default
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="dates must be YYYY-MM-DD") from exc

    # ---------------------------------------------------------------- pages
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, error: str | None = None):
        return page(request, "login.html", None, error=error)

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...), _=Depends(check_csrf)):
        if limiter.blocked(email):
            return back("/login", error="Too many attempts. Try again in 15 minutes.")
        user = db.employee_by_email(email)
        if not user or not user["active"] or not verify_password(password, user["password_hash"]):
            limiter.fail(email)
            return back("/login", error="Wrong email or password.")
        limiter.reset(email)
        request.session.clear()
        request.session.update(uid=user["id"], csrf=new_csrf_token())
        return RedirectResponse("/" if user["role"] != "employee" else "/me", status_code=303)

    @app.post("/logout")
    def logout(request: Request, _=Depends(check_csrf)):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request, day: str | None = None, team: str | None = None, user: dict = Depends(current_user)
    ):
        if user["role"] == "employee":
            return RedirectResponse("/me", status_code=303)
        d = parse_day(day, today())
        teams = sorted({e["team"] for e in db.employees(active_only=True)})
        return page(
            request,
            "dashboard.html",
            user,
            dash=reports.dashboard(d, clock(), team or None),
            day=d,
            is_today=d == today(),
            teams=teams,
            team=team or "",
            prev=d - timedelta(days=1),
            next=d + timedelta(days=1),
        )

    @app.get("/me", response_class=HTMLResponse)
    def me(request: Request, msg: str | None = None, error: str | None = None, user: dict = Depends(current_user)):
        end = today()
        start = end - timedelta(days=end.weekday())  # this week, Monday first
        lo, _ = day_bounds(start - timedelta(days=7), policy.tz)
        _, hi = day_bounds(end, policy.tz)
        sessions = db.sessions_between(lo, hi, user["id"])
        leaves = db.leave_days(start, end)
        own, hol = leaves.get(user["id"], set()), leaves.get(None, set())
        week = summarize_period(user["id"], user["expected_hours"], sessions, start, end, policy, clock(), own, hol)
        open_s = db.open_session(user["id"])
        return page(
            request,
            "me.html",
            user,
            week=week,
            today=week.days[-1],
            open_session=open_s,
            sessions=list(reversed(sessions))[:15],
            msg=msg,
            error=error,
        )

    @app.post("/me/clock")
    def clock_toggle(user: dict = Depends(current_user), _=Depends(check_csrf)):
        now = clock()
        try:
            if db.open_session(user["id"]):
                db.clock_out(user["id"], now)
                return back("/me", msg="Clocked out. See you tomorrow!")
            db.clock_in(user["id"], now)
            return back("/me", msg="Clocked in. Have a good day!")
        except ValueError as exc:
            return back("/me", error=str(exc))

    @app.get("/employees", response_class=HTMLResponse)
    def employees(
        request: Request, msg: str | None = None, error: str | None = None, user: dict = Depends(require("manager"))
    ):
        return page(request, "employees.html", user, employees=db.employees(), msg=msg, error=error, roles=ROLES)

    @app.post("/employees")
    def add_employee(
        name: str = Form(...),
        email: str = Form(...),
        role: str = Form("employee"),
        team: str = Form("General"),
        expected_hours: float = Form(8.0),
        password: str = Form(...),
        user: dict = Depends(require("admin")),
        _=Depends(check_csrf),
    ):
        if role not in ROLES or not 0 <= expected_hours <= 24 or "@" not in email:
            return back("/employees", error="Check the role, email and expected hours (0-24).")
        if db.employee_by_email(email):
            return back("/employees", error="That email is already registered.")
        try:
            db.add_employee(name, email, role, team, expected_hours, hash_password(password))
        except ValueError as exc:
            return back("/employees", error=str(exc))
        return back("/employees", msg=f"Added {name}.")

    @app.post("/employees/{employee_id}/active")
    def toggle_active(employee_id: int, user: dict = Depends(require("admin")), _=Depends(check_csrf)):
        e = db.employee(employee_id)
        if not e:
            raise HTTPException(status_code=404, detail="no such employee")
        if e["id"] == user["id"]:
            return back("/employees", error="You cannot deactivate yourself.")
        db.set_active(employee_id, not e["active"])
        return back("/employees", msg=f"{e['name']} {'deactivated' if e['active'] else 'reactivated'}.")

    @app.get("/employees/{employee_id}", response_class=HTMLResponse)
    def employee_detail(
        request: Request,
        employee_id: int,
        msg: str | None = None,
        error: str | None = None,
        user: dict = Depends(require("manager")),
    ):
        e = db.employee(employee_id)
        if not e:
            raise HTTPException(status_code=404, detail="no such employee")
        end = today()
        start = end - timedelta(days=13)
        lo, _ = day_bounds(start, policy.tz)
        _, hi = day_bounds(end, policy.tz)
        sessions = db.sessions_between(lo, hi, employee_id)
        leaves = db.leave_days(start, end)
        own, hol = leaves.get(employee_id, set()), leaves.get(None, set())
        period = summarize_period(employee_id, e["expected_hours"], sessions, start, end, policy, clock(), own, hol)
        return page(
            request,
            "employee.html",
            user,
            e=dict(e),
            period=period,
            sessions=list(reversed(sessions))[:20],
            leaves=db.leaves_for(employee_id),
            msg=msg,
            error=error,
            today=end,
        )

    @app.post("/employees/{employee_id}/sessions")
    def add_session(
        employee_id: int,
        day: str = Form(...),
        start: str = Form(...),
        end: str = Form(...),
        note: str = Form(""),
        user: dict = Depends(require("manager")),
        _=Depends(check_csrf),
    ):
        url = f"/employees/{employee_id}"
        try:
            d = date.fromisoformat(day)
            s = datetime.combine(d, time.fromisoformat(start), tzinfo=policy.tz)
            e_ = datetime.combine(d, time.fromisoformat(end), tzinfo=policy.tz)
            if e_ <= s:
                e_ += timedelta(days=1)  # overnight shift
            if e_ > clock():
                return back(url, error="Entries cannot end in the future.")
            db.add_manual_session(employee_id, s, e_, note or f"added by {user['name']}")
        except ValueError as exc:
            return back(url, error=str(exc))
        return back(url, msg="Time entry added.")

    @app.post("/employees/{employee_id}/leave")
    def add_leave(
        employee_id: int,
        day: str = Form(...),
        kind: str = Form("leave"),
        note: str = Form(""),
        user: dict = Depends(require("manager")),
        _=Depends(check_csrf),
    ):
        url = f"/employees/{employee_id}"
        if kind not in ("leave", "sick"):
            return back(url, error="Leave type must be leave or sick.")
        try:
            db.add_leave(employee_id, date.fromisoformat(day), kind, note)
        except ValueError:
            return back(url, error="Pick a valid date.")
        return back(url, msg="Leave recorded.")

    @app.post("/holidays")
    def add_holiday(
        day: str = Form(...), note: str = Form(""), user: dict = Depends(require("admin")), _=Depends(check_csrf)
    ):
        try:
            db.add_leave(None, date.fromisoformat(day), "holiday", note)
        except ValueError:
            return back("/employees", error="Pick a valid date.")
        return back("/employees", msg="Company holiday added.")

    @app.get("/reports", response_class=HTMLResponse)
    def report_page(
        request: Request, start: str | None = None, end: str | None = None, user: dict = Depends(require("manager"))
    ):
        e = parse_day(end, today())
        s = parse_day(start, e - timedelta(days=e.weekday()))
        if e < s:
            raise HTTPException(status_code=400, detail="end date is before start date")
        if (e - s).days > 366:
            raise HTTPException(status_code=400, detail="pick at most one year")
        rows = reports.period(s, e, clock())
        totals = {k: round(sum(getattr(p, k) for _, p in rows), 2) for k in ("worked_h", "expected_h", "not_worked_h")}
        return page(request, "reports.html", user, rows=rows, start=s, end=e, totals=totals)

    @app.get("/reports.csv")
    def report_csv(start: str | None = None, end: str | None = None, user: dict = Depends(require("manager"))):
        e = parse_day(end, today())
        s = parse_day(start, e - timedelta(days=e.weekday()))
        if e < s:
            raise HTTPException(status_code=400, detail="end date is before start date")
        return PlainTextResponse(
            reports.period_csv(s, e, clock()),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="teampulse_{s}_{e}.csv"'},
        )

    # ------------------------------------------------------------------ API
    @app.get("/api/v1/dashboard")
    def api_dashboard(day: str | None = None, team: str | None = None, user: dict = Depends(require("manager"))):
        return reports.dashboard(parse_day(day, today()), clock(), team).as_dict()

    @app.get("/api/v1/trend")
    def api_trend(days: int = 14, user: dict = Depends(require("manager"))):
        return {"days": reports.trend(max(1, min(days, 90)), clock())}

    @app.get("/api/v1/me")
    def api_me(user: dict = Depends(current_user)):
        d = reports.dashboard(today(), clock())
        mine = next((x for x in d.as_dict()["employees"] if x["id"] == user["id"]), None)
        return {"id": user["id"], "name": user["name"], "role": user["role"], "today": mine}

    @app.get("/healthz")
    def healthz() -> Response:
        db.employees()
        return PlainTextResponse("ok")

    return app


def create_app_from_env() -> FastAPI:
    return create_app(Settings.from_env())
