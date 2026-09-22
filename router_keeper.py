"""router_keeper.py - keep 9router, the brain's backend, running.

Only two things started 9router: the Startup-folder script at login and the
desktop app when it opens. Once it stopped (a crash, or quit from its tray)
nothing brought it back, and every turn after that failed with "Connection
error" and dropped to the slow local model without a word (2026-09-22).

ensure_up() starts it when the port is shut and waits for it; the brain calls
it when a router call cannot connect, and watch() checks every WATCH_EVERY_S
seconds. A 9router process that is already starting (the login script, the
desktop app) is waited for, never started a second time.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

WATCH_EVERY_S = float(os.getenv("JARVIS_ROUTER_WATCH_S", "30"))
# Measured ~12 s from a cold start to the port answering (2026-09-16).
START_WAIT_S = 20.0
# After one start, the next may wait this long: a router that dies at once
# must not be restarted on every turn.
RESTART_GAP_S = 60.0

# Off under pytest (a test turns it on with a fake _spawn) and with
# JARVIS_ROUTER_KEEP=0: then ensure_up only reports whether the port answers.
ENABLED = "pytest" not in sys.modules and os.getenv("JARVIS_ROUTER_KEEP", "1") != "0"

_lock = threading.Lock()
_last_start = 0.0
_thread = None
state = {"up": None, "started": 0, "last_error": ""}   # read by the HUD notice


def _address():
    try:
        from config import ROUTER_BASE_URL
    except Exception:
        ROUTER_BASE_URL = "http://localhost:20128/v1"
    u = urlparse(ROUTER_BASE_URL)
    return u.hostname or "localhost", u.port or 20128


def port_open(timeout: float = 0.5) -> bool:
    host, port = _address()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_connection_error(err) -> bool:
    """A call that never reached 9router: the router is down, not the model."""
    text = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err or "")
    return any(k in text for k in ("APIConnectionError", "Connection error", "ConnectError",
                                   "Connection refused", "actively refused", "WinError 10061"))


def _router_process_running() -> bool:
    """A 9router node process exists (it may still be starting up)."""
    try:
        import psutil
    except ImportError:
        return False
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if "node" in (p.info.get("name") or "").lower() and "9router" in cmd \
                    and ("cli.js" in cmd or "custom-server.js" in cmd):
                return True
        except Exception:
            continue
    return False


def _command():
    """The login script's command, with the port Cygnus uses."""
    cmd = os.path.join(os.environ.get("APPDATA", ""), "npm", "9router.cmd")
    if not os.path.isfile(cmd):
        return None
    _, port = _address()
    # "start" runs it from a cmd that exits at once, so 9router is nobody's
    # child: the desktop app stops the backend with taskkill /T, which took
    # a router the backend had started down with it, and Hermes and the
    # coding CLIs share that router.
    return [os.environ.get("COMSPEC", "cmd.exe"), "/c", "start", "", "/b", cmd,
            "--tray", "--no-browser", "--skip-update", "--port", str(port)]


def _spawn(cmd) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(cmd, cwd=os.path.expanduser("~"), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
                     close_fds=True)


def _wait(seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if port_open():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def ensure_up(wait: float = START_WAIT_S) -> bool:
    """True once 9router answers on its port; starts it if it is not running.
    Waits at most `wait` seconds (0: start it, do not wait)."""
    global _last_start
    if port_open():
        state.update(up=True, last_error="")
        return True
    if not ENABLED:
        state.update(up=False, last_error="9router is not running")
        return False
    with _lock:
        if port_open():
            state.update(up=True, last_error="")
            return True
        now = time.monotonic()
        starting = _router_process_running() or (_last_start and now - _last_start < START_WAIT_S)
        if not starting and (not _last_start or now - _last_start >= RESTART_GAP_S):
            cmd = _command()
            if cmd is None:
                state.update(up=False, last_error="9router is not installed (npm i -g 9router)")
                return False
            try:
                _spawn(cmd)
                _last_start = now
                state["started"] += 1
                print("[ROUTER] 9router was not running; started it", flush=True)
            except OSError as e:
                state.update(up=False, last_error=f"could not start 9router: {e}")
                return False
    up = _wait(wait)
    state.update(up=up, last_error="" if up else "9router did not come up")
    return up


def _loop():
    while True:
        try:
            ensure_up(wait=0)
        except Exception as e:
            print(f"[ROUTER] keeper check failed: {e}", flush=True)
        time.sleep(WATCH_EVERY_S)


def watch() -> None:
    """Check the router every WATCH_EVERY_S seconds (once per process, never
    under pytest; JARVIS_ROUTER_KEEP=0 turns it off)."""
    global _thread
    if _thread is not None or not ENABLED:
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="router-keeper")
    _thread.start()
