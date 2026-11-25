"""Settings from environment variables (the same names in .env, docker-compose.yml and the Helm-free k8s manifest)."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .timecalc import Policy


@dataclass(frozen=True)
class Settings:
    db_path: str
    tz: str
    workdays: frozenset[int]
    under_ratio: float
    secret_key: str
    secure_cookies: bool
    company: str

    @property
    def policy(self) -> Policy:
        return Policy(tz=ZoneInfo(self.tz), workdays=self.workdays, under_ratio=self.under_ratio)

    @classmethod
    def from_env(cls) -> Settings:
        secret = os.environ.get("TEAMPULSE_SECRET_KEY", "")
        if not secret:
            secret = secrets.token_urlsafe(32)
            print(
                "teampulse: TEAMPULSE_SECRET_KEY not set; using a random key (sessions reset on restart)",
                file=sys.stderr,
            )
        days = os.environ.get("TEAMPULSE_WORKDAYS", "0,1,2,3,4")
        return cls(
            db_path=os.environ.get("TEAMPULSE_DB", "data/teampulse.db"),
            tz=os.environ.get("TEAMPULSE_TZ", "Asia/Kolkata"),
            workdays=frozenset(int(d) for d in days.split(",") if d.strip()),
            under_ratio=float(os.environ.get("TEAMPULSE_UNDER_RATIO", "0.75")),
            secret_key=secret,
            secure_cookies=os.environ.get("TEAMPULSE_SECURE_COOKIES", "false").lower() in {"1", "true", "yes"},
            company=os.environ.get("TEAMPULSE_COMPANY", "Your Startup"),
        )
