#!/usr/bin/env python3
"""Single source of truth for the pipeline (GitHub Actions, Jenkins and laptops all run these).

    python ci.py lint      ruff lint + format check
    python ci.py unit      tests with coverage >= 90%
    python ci.py build     container image
    python ci.py scan      Trivy: fail on fixable HIGH/CRITICAL CVEs
    python ci.py smoke     run the image, seed demo data, sign in over HTTP, check pages and API
    python ci.py k8s       deploy deploy/kubernetes.yaml to a k3d cluster and repeat the smoke test there
    python ci.py all       everything, in order

Environment: K3D_CLUSTER (default "dev").
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
IMAGE = "teampulse:local"
TRIVY = "aquasec/trivy@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"
CLUSTER = os.environ.get("K3D_CLUSTER", "dev")
CONTEXT, NS = f"k3d-{CLUSTER}", "teampulse"
DEMO_ADMIN, DEMO_PASSWORD = "ritu@demo.teampulse.dev", "teampulse-demo"  # seeded demo account (not a secret)


def run(*cmd: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    res = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if check and res.returncode != 0:
        if capture:
            print(res.stdout, res.stderr, sep="\n")
        raise SystemExit(f"command failed ({res.returncode}): {' '.join(cmd)}")
    return res


def lint() -> None:
    run(PY, "-m", "ruff", "check", "src", "tests", "ci.py")
    run(PY, "-m", "ruff", "format", "--check", "src", "tests", "ci.py")


def unit() -> None:
    run(
        PY,
        "-m",
        "pytest",
        "-q",
        "--cov=teampulse",
        "--cov-report=term-missing",
        "--cov-fail-under=90",
        "--junitxml=reports/unit.xml",
    )


def build() -> None:
    run("docker", "build", "-t", IMAGE, ".")


def scan() -> None:
    cache = ROOT / ".trivy-cache"
    cache.mkdir(exist_ok=True)
    run(
        "docker",
        "run",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{cache}:/root/.cache/trivy",
        TRIVY,
        "image",
        "--quiet",
        "--exit-code",
        "1",
        "--severity",
        "HIGH,CRITICAL",
        "--ignore-unfixed",
        "--scanners",
        "vuln",
        IMAGE,
    )


def _http_check(base: str) -> None:
    """Sign in as the demo admin and check the main pages and the API through real HTTP."""
    deadline = time.monotonic() + 90
    while True:
        try:
            if urllib.request.urlopen(base + "/healthz", timeout=5).read() == b"ok":
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"{base} never became healthy")
        time.sleep(2)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login_page = opener.open(base + "/login", timeout=10).read().decode()
    token = re.search(r'name="csrf" value="([^"]+)"', login_page).group(1)
    form = urllib.parse.urlencode({"email": DEMO_ADMIN, "password": DEMO_PASSWORD, "csrf": token}).encode()
    landing = opener.open(base + "/login", data=form, timeout=10)
    assert landing.geturl().rstrip("/") == base, f"login did not reach the dashboard: {landing.geturl()}"
    body = landing.read().decode()
    assert "Hours not worked" in body and "Working now" in body, "dashboard did not render"
    data = json.loads(opener.open(base + "/api/v1/dashboard", timeout=10).read())
    assert data["headcount"] == 10, data
    for path in ("/reports", "/employees", "/me", "/reports.csv", "/api/v1/trend?days=14"):
        status = opener.open(base + path, timeout=10).status
        assert status == 200, f"{path} -> {status}"
    print(
        f"smoke: signed in, dashboard shows {data['headcount']} people, "
        f"{data['working_now']} working now, utilization {data['utilization']}"
    )


def smoke() -> None:
    name, vol = "teampulse-smoke", "teampulse-smoke-data"
    run("docker", "rm", "-f", name, check=False, capture=True)
    run("docker", "volume", "rm", "-f", vol, check=False, capture=True)
    try:
        run("docker", "run", "--rm", "-v", f"{vol}:/data", IMAGE, "seed-demo")
        run(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--memory",
            "256m",
            "-p",
            "18000:8000",
            "-v",
            f"{vol}:/data",
            "-e",
            "TEAMPULSE_SECRET_KEY=ci-smoke-only",
            IMAGE,
        )
        _http_check("http://127.0.0.1:18000")
    finally:
        run("docker", "rm", "-f", name, check=False, capture=True)
        run("docker", "volume", "rm", "-f", vol, check=False, capture=True)


def k8s() -> None:
    kc = ["kubectl", "--context", CONTEXT, "-n", NS]
    clusters = json.loads(run("k3d", "cluster", "list", "-o", "json", capture=True).stdout or "[]")
    if CLUSTER not in {c["name"] for c in clusters}:
        run("k3d", "cluster", "create", CLUSTER, "--agents", "0", "--wait")
    run("k3d", "image", "import", "-c", CLUSTER, IMAGE)
    run("kubectl", "--context", CONTEXT, "delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    run("kubectl", "--context", CONTEXT, "create", "namespace", NS)
    run(*kc, "create", "secret", "generic", "teampulse", "--from-literal=TEAMPULSE_SECRET_KEY=ci-k8s-only")
    run(*kc, "apply", "-f", "deploy/kubernetes.yaml")
    run(*kc, "rollout", "status", "deployment/teampulse", "--timeout=180s")
    run(*kc, "exec", "deploy/teampulse", "--", "python", "-m", "teampulse", "seed-demo")
    pf = subprocess.Popen(
        [*kc, "port-forward", "svc/teampulse", "18001:80"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _http_check("http://127.0.0.1:18001")
        print("k8s: deployment healthy, data on the PersistentVolume, app reachable through the Service")
    finally:
        pf.terminate()
        run("kubectl", "--context", CONTEXT, "delete", "namespace", NS, "--wait=false", check=False)


STAGES = {"lint": lint, "unit": unit, "build": build, "scan": scan, "smoke": smoke, "k8s": k8s}


def main(argv: list[str]) -> int:
    names = list(STAGES) if argv in ([], ["all"]) else argv
    unknown = [n for n in names if n not in STAGES]
    if unknown:
        print(f"unknown stage(s) {unknown}; choose from {list(STAGES)} or all")
        return 2
    (ROOT / "reports").mkdir(exist_ok=True)
    for n in names:
        t0 = time.monotonic()
        print(f"\n===== stage: {n} =====", flush=True)
        STAGES[n]()
        print(f"===== {n} passed in {time.monotonic() - t0:.1f}s =====", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
