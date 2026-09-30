"""Stretch goal demo: run the capability catalog as a real HTTP service and
call it the way an external AI agent would -- genuine network requests against
a genuine uvicorn process, not an in-process test client.

    python scripts/demo_catalog.py

Starts `uvicorn cua.catalog:app` as a subprocess (which itself starts the mock
bank app), lists the catalog, fetches a tool schema, and invokes one
capability for real. Writes the exchange to evidence/catalog/.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PORT = 8008
BASE = f"http://127.0.0.1:{PORT}"
EVIDENCE = Path("evidence/catalog")


def _get(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{BASE}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _post(path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _stop(proc: subprocess.Popen) -> None:
    """Tree-kill: uvicorn's own child (the mock bank app it starts in its
    lifespan) would otherwise be orphaned -- proc.terminate() on Windows kills
    only the one PID, not the tree, and doesn't run FastAPI's shutdown."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_up(timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{BASE}/health", timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("catalog service did not come up")


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cua.catalog:app",
         "--port", str(PORT), "--log-level", "warning"],
    )
    exchange = []
    try:
        _wait_up()

        for label, call in [
            ("health", lambda: _get("/health")),
            ("list_capabilities", lambda: _get("/capabilities")),
            ("tool_schema", lambda: _get("/capabilities/read_member_balance/tool_schema")),
            ("invoke", lambda: _post("/capabilities/read_member_balance/invoke", {
                "inputs": {"member_id": "100234", "account_type": "Savings"}})),
        ]:
            status, body = call()
            print(f"\n=== {label} ({status}) ===")
            print(json.dumps(body, indent=2)[:1500])
            exchange.append({"call": label, "status": status, "body": body})
    finally:
        _stop(proc)

    (EVIDENCE / "http_exchange.json").write_text(
        json.dumps(exchange, indent=2), encoding="utf-8")
    print(f"\nwrote {EVIDENCE / 'http_exchange.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
