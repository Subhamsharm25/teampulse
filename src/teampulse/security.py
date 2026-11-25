"""Password hashing (scrypt, standard library), CSRF tokens and a small login rate limiter."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(expected: str | None, supplied: str | None) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


class LoginLimiter:
    """At most `attempts` failed logins per email per `window_s`; protects against password guessing."""

    def __init__(self, attempts: int = 5, window_s: float = 900, clock=time.monotonic) -> None:
        self.attempts, self.window_s, self._clock = attempts, window_s, clock
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        now = self._clock()
        recent = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        self._fails[key] = recent
        return recent

    def blocked(self, email: str) -> bool:
        with self._lock:
            return len(self._recent(email.lower())) >= self.attempts

    def fail(self, email: str) -> None:
        with self._lock:
            self._recent(email.lower()).append(self._clock())

    def reset(self, email: str) -> None:
        with self._lock:
            self._fails.pop(email.lower(), None)
