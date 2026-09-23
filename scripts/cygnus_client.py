"""cygnus_client.py - talk to a running Cygnus the way the HUD does.

Shared by scripts/cygnus_smoke.py and scripts/cygnus_stress.py: find the
server, send a typed command over /ws and collect its reply, and read which
tools ran from logs/audit.jsonl (the server runs on this machine).
"""

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

import httpx
import websockets

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from cygnus import paths  # noqa: E402

AUDIT = paths.log("audit.jsonl")
CANDIDATES = ("http://127.0.0.1:8001", "http://127.0.0.1:8000")


def _configured_port():
    """JARVIS_PORT from the environment, else from the repo's .env."""
    port = os.environ.get("JARVIS_PORT")
    if not port:
        try:
            with open(os.path.join(REPO, ".env"), encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("JARVIS_PORT="):
                        port = line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            pass
    return port if port and port.isdigit() else None


def _listening_ports():
    """Ports a running `python -m cygnus` is listening on.

    The configured port is not always the one in use: on 2026-09-23 a Laravel
    dev server held 8001, Cygnus came up on 8002, and every bench script
    reported "no Cygnus server" while one was answering turns.
    """
    try:
        import psutil
    except ImportError:
        return ()
    ports = []
    for p in psutil.process_iter(["name", "cmdline"], ad_value=None):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if not re.search(r"-m\s+cygnus(\s|$)", cmd) or "soak.py" in cmd:
                continue
            for c in p.net_connections(kind="inet"):
                if c.status == psutil.CONN_LISTEN and c.laddr.port:
                    ports.append(c.laddr.port)
        except Exception:
            continue
    return tuple(dict.fromkeys(ports))


def candidates():
    port = _configured_port()
    first = (f"http://127.0.0.1:{port}",) if port else ()
    found = tuple(f"http://127.0.0.1:{p}" for p in _listening_ports())
    return tuple(dict.fromkeys(first + CANDIDATES + found))


def _is_cygnus(base):
    """/status answers with Cygnus's own fields. Another dev server on the
    port (a Laravel app held 8001 on 2026-09-22) may answer 200 too."""
    try:
        r = httpx.get(base.rstrip("/") + "/status", timeout=3)
        return r.status_code == 200 and "brain" in r.json()
    except (httpx.HTTPError, ValueError):
        return False


def find_server(url=None):
    """Base URL of the running server; exits with a hint when none answers."""
    tried = [url] if url else list(candidates())
    for base in tried:
        if _is_cygnus(base):
            return base.rstrip("/")
    raise SystemExit(f"No Cygnus server answered on {' or '.join(tried)}. Start it first.")


def connect(base):
    url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    return websockets.connect(url, max_size=None, open_timeout=10, ping_interval=None)


async def hello(ws, session):
    """Join a conversation of our own: Cygnus keeps one per client, so test
    turns stay out of the user's history (a server that predates sessions
    ignores this)."""
    await ws.send(json.dumps({"type": "hello", "session": session}))


@dataclass
class Turn:
    said: str
    reply: str | None = None
    error: str | None = None
    telemetry: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    late: list = field(default_factory=list)      # replies to earlier background work
    ms: int = 0


def _take(turn, msg):
    """Fold one server message into the turn; True when the turn is over."""
    kind = msg.get("type")
    turn.events.append(kind)
    if kind == "response" and (msg.get("proactive") or msg.get("deferred")):
        turn.late.append(msg.get("text") or "")
    elif kind == "response":
        turn.reply = msg.get("text") or ""
    elif kind == "telemetry":
        turn.telemetry = msg.get("stats") or {}
    elif kind == "error":
        turn.error = msg.get("text") or "error"
        return True
    return kind == "status" and msg.get("state") == "idle" and turn.reply is not None


async def ask(ws, text, timeout=60.0):
    """Send one typed command; wait for its reply and the idle that follows."""
    turn = Turn(said=text)
    start = time.monotonic()
    await ws.send(json.dumps({"type": "text", "text": text}))
    while True:
        left = timeout - (time.monotonic() - start)
        if left <= 0:
            turn.error = f"no reply within {timeout:.0f}s"
            break
        try:
            msg = json.loads(await asyncio.wait_for(ws.recv(), left))
        except asyncio.TimeoutError:
            continue
        if _take(turn, msg):
            break
    turn.ms = int((time.monotonic() - start) * 1000)
    return turn


def audit_mark():
    try:
        return os.path.getsize(AUDIT)
    except OSError:
        return 0


# Audit decisions that record a request without running it: a gated delete
# logs delegate_to_hermes with needs_confirm, then declined.
_NOT_RUN = {"brief_assembled", "needs_confirm", "needs_confirm_latch_mismatch", "declined",
            "ambiguous", "blocked_scope", "blocked_exists", "unknown_tool", "quarantined"}


def tools_since(mark, path=None):
    """Names of the tools that ran, from audit lines after byte offset `mark`."""
    try:
        with open(path or AUDIT, "rb") as f:
            f.seek(mark)
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    names = []
    for line in chunk.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        name = rec.get("tool") or (rec.get("caller") or "").replace("execute_tool:", "")
        if name and str(rec.get("decision", "")).lower() not in _NOT_RUN:
            names.append(name)
    return names


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def report_path(kind):
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    return paths.log(f"cygnus-{kind}-{time.strftime('%Y%m%d-%H%M%S')}.json")
