"""Spin the mock bank app up and down as a subprocess.

Used by the discovery / replay CLIs so a run is self-contained. Windows-safe:
the child is started in its own process group and the whole tree is killed on
exit (Flask's dev server spawns a reloader child even with reload off on some
setups).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def running_bankapp(*, base_url: str = "http://127.0.0.1:5001", env_extra: dict | None = None,
                    reset: bool = True):
    from urllib.parse import urlparse

    _clear_stale(base_url)
    port = urlparse(base_url).port or 5001
    env = {**os.environ, "BANKAPP_PORT": str(port), **(env_extra or {})}
    creationflags = 0
    preexec_fn = None
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        preexec_fn = os.setsid  # type: ignore[assignment]

    proc = subprocess.Popen(
        [sys.executable, str(_ROOT / "run_bankapp.py")],
        cwd=str(_ROOT), env=env, creationflags=creationflags, preexec_fn=preexec_fn,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_up(base_url, timeout=15)
        if reset:
            with contextlib.suppress(Exception):
                urllib.request.urlopen(f"{base_url}/admin/reset", data=b"", timeout=5)
        yield base_url
    finally:
        _kill(proc)


def _clear_stale(base_url: str) -> None:
    """If a previous run's server is still bound to the port (its `finally`
    didn't fire -- a hard-killed parent process, an interrupted debug
    session), a fresh Popen would silently bind to nothing and we'd end up
    talking to the leftover process with the wrong env vars. Best-effort
    kill it first."""
    import socket
    from urllib.parse import urlparse

    host, port = urlparse(base_url).hostname, urlparse(base_url).port
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass  # something is listening -- fall through and clear it
    except OSError:
        return  # port is free, nothing to do

    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                 f"-ErrorAction SilentlyContinue | "
                 f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force "
                 f"-ErrorAction SilentlyContinue }}"],
                capture_output=True, timeout=10,
            )
            time.sleep(0.5)


def _wait_up(base_url: str, timeout: float) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/members/search", timeout=2)
            return
        except Exception as ex:  # noqa: BLE001
            last = ex
            time.sleep(0.3)
    raise RuntimeError(f"bank app did not come up at {base_url}: {last}")


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(Exception):
        if sys.platform == "win32":
            # go straight to a forceful tree-kill; CTRL_BREAK_EVENT alone was
            # observed to leave the server running on some setups
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    if proc.poll() is None:
        with contextlib.suppress(Exception):
            proc.kill()
            proc.wait(timeout=5)
