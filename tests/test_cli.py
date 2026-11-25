from teampulse.__main__ import main
from teampulse.config import Settings
from teampulse.db import Database


def test_seed_demo_create_admin_and_settings(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("TEAMPULSE_DB", str(db_path))
    monkeypatch.setenv("TEAMPULSE_WORKDAYS", "0,1,2,3,4,5")
    monkeypatch.setenv("TEAMPULSE_SECRET_KEY", "k")
    s = Settings.from_env()
    assert s.workdays == frozenset(range(6)) and s.db_path == str(db_path) and s.policy.tz.key == "Asia/Kolkata"

    assert main(["seed-demo"]) == 0
    assert "seeded 10 people" in capsys.readouterr().out
    assert main(["seed-demo"]) == 1  # refuses a non-empty database

    monkeypatch.setenv("TEAMPULSE_ADMIN_PASSWORD", "founder-pass-123")
    assert main(["create-admin", "--name", "Dev Founder", "--email", "dev@acme.in"]) == 0
    assert main(["create-admin", "--name", "Dev Founder", "--email", "dev@acme.in"]) == 1
    assert Database(db_path).employee_by_email("DEV@acme.in")["role"] == "admin"


def test_missing_secret_falls_back_to_random(monkeypatch, capsys):
    monkeypatch.delenv("TEAMPULSE_SECRET_KEY", raising=False)
    a, b = Settings.from_env(), Settings.from_env()
    assert a.secret_key != b.secret_key and "not set" in capsys.readouterr().err


def test_serve_starts_uvicorn_with_the_factory(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(a=a, k=k))
    assert main(["serve", "--port", "9001"]) == 0
    assert seen["a"] == ("teampulse.app:create_app_from_env",) and seen["k"]["factory"] and seen["k"]["port"] == 9001
