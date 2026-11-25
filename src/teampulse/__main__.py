"""Command line.

python -m teampulse serve [--host 0.0.0.0] [--port 8000]
python -m teampulse create-admin --name "Ritu Kapoor" --email ritu@acme.in   (asks for the password)
python -m teampulse seed-demo        (10 demo people, 4 weeks of history; empty database only)
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import Settings
from .db import Database
from .security import hash_password
from .timecalc import utc_now


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="teampulse")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    a = sub.add_parser("create-admin")
    a.add_argument("--name", required=True)
    a.add_argument("--email", required=True)
    a.add_argument("--team", default="Leadership")
    sub.add_parser("seed-demo")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("teampulse.app:create_app_from_env", factory=True, host=args.host, port=args.port)
        return 0

    settings = Settings.from_env()
    db = Database(settings.db_path)
    if args.cmd == "create-admin":
        if db.employee_by_email(args.email):
            print("that email already exists", file=sys.stderr)
            return 1
        password = os.environ.get("TEAMPULSE_ADMIN_PASSWORD") or getpass.getpass("Password (min 8 chars): ")
        db.add_employee(args.name, args.email, "admin", args.team, 8, hash_password(password))
        print(f"admin {args.email} created")
        return 0

    from .seed import DEMO_PASSWORD, seed

    try:
        result = seed(db, settings.policy, utc_now())
    except ValueError as exc:
        print(f"seed-demo: {exc}", file=sys.stderr)
        return 1
    print(f"seeded {result['employees']} people and {result['sessions']} time entries")
    print(
        f"demo logins: ritu@demo.teampulse.dev (admin), meera@demo.teampulse.dev (manager), "
        f"priya@demo.teampulse.dev (employee); password: {DEMO_PASSWORD}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
