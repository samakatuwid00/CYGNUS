"""incidents.py - what counts as something going wrong, in a turn or in the log.

Every turn leaves one line in data/logs/turns.jsonl (record_turn, called by
JarvisBrain.think): what was said, the reply, which backend and model answered,
how long it took, and the exception if the turn raised. scripts/watch_incidents.py
follows that file and the backend's log, asks turn_problems() and LogScanner
whether each is a problem, and writes the problems to data/logs/incidents.jsonl.

On 2026-09-29 six things went wrong in an hour of chatting and none of them was
written down anywhere a person would look: "My model didn't answer in time"
three times, "Then my model stopped answering" twice, and 9router's error text
shown as the reply.

The rules are here, with no I/O but the turn record, so tests can hold them.
"""

from __future__ import annotations

import datetime
import json
import os
import re

from cygnus import paths

TURNS_PATH = paths.log("turns.jsonl")
INCIDENTS_PATH = paths.log("incidents.jsonl")

# A turn slower than this is a problem even when it answered: the 67 s one on
# 2026-09-29 walked eight tools and two models before giving up.
SLOW_TURN_S = float(os.getenv("CYGNUS_SLOW_TURN_S", "45"))

_TEXT_CAP = 600


def record_turn(user: str, reply, stats: dict | None, backend, seconds: float,
                client: str | None = None, error: BaseException | None = None,
                path: str | None = None) -> None:
    """One line per turn. Never raises: a turn must not fail on its own record."""
    try:
        stats = stats or {}
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "client": client or "default",
            "user": str(user or "")[:_TEXT_CAP],
            "reply": None if reply is None else str(reply)[:_TEXT_CAP],
            "backend": stats.get("backend") or backend,
            "model": stats.get("model"),
            "seconds": round(float(seconds), 2),
            "broke": stats.get("broke"),
            "tools": list(stats.get("tools") or [])[:20],
        }
        if error is not None:
            rec["error"] = f"{type(error).__name__}: {error}"[:_TEXT_CAP]
        path = path or TURNS_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# Replies that are Cygnus saying it failed, in its own words (core.py
# _why_no_model / _NO_MODEL_REPLY, agent_loop "stopped answering").
_FAILED_REPLY_RE = re.compile(
    r"didn't answer in time|stopped answering|no model (?:could|answered)|"
    r"couldn't reach the model endpoint|couldn't get an answer from my AI model|"
    r"my model is out of quota|answered with nothing|ran out of steps", re.I)
# An error handed back as the reply: a tool's or a provider's.
_ERROR_REPLY_RE = re.compile(
    r"^\s*\[(?:Error|Tool Error)\]|^\s*\[[\w.-]+ error(?: \d{3})?:|"
    r"^\s*\[[\w.-]+/[\w.:-]+\] (?:\[\d{3}\]|Unavailable\b)|Traceback \(most recent call last\)")


def turn_problems(rec: dict) -> list[dict]:
    """The problems in one turn record, most serious first; [] for a good turn."""
    out = []
    reply = rec.get("reply")
    if rec.get("error"):
        out.append({"kind": "turn_crashed", "severity": "error", "detail": rec["error"]})
    elif rec.get("backend") == "no-model":
        out.append({"kind": "no_model", "severity": "error", "detail": reply or ""})
    elif reply is None or not str(reply).strip():
        out.append({"kind": "empty_reply", "severity": "error", "detail": ""})
    elif _ERROR_REPLY_RE.search(reply):
        out.append({"kind": "error_reply", "severity": "error", "detail": reply[:300]})
    elif _FAILED_REPLY_RE.search(reply):
        out.append({"kind": "model_stopped", "severity": "warning", "detail": reply[:300]})
    if (rec.get("seconds") or 0) > SLOW_TURN_S:
        out.append({"kind": "slow_turn", "severity": "warning",
                    "detail": f"{rec['seconds']:.0f}s (over {SLOW_TURN_S:.0f}s)"
                              + (f", tools: {', '.join(rec['tools'])}" if rec.get("tools") else "")})
    return out


# Backend log lines that are a problem on their own. A model failing inside a
# turn is not one: the next model answers, and a dead 9router model fails every
# turn for days. The turn record says whether the turn itself went wrong.
_LINE_RULES = [
    (re.compile(r"^\[agent\] failed \("), "agent_crashed", "error"),
    (re.compile(r"^Exception in ASGI application"), "server_error", "error"),
    (re.compile(r"^\[WS\] (?:error|telemetry error|model notice failed)"), "websocket_error", "warning"),
    (re.compile(r"^\[shadow\] observer failed"), "observer_failed", "warning"),
    (re.compile(r"^\[JARVIS\] confirm-release failed"), "confirm_failed", "warning"),
]
_TRACEBACK = "Traceback (most recent call last):"
# Exceptions that are Windows' asyncio talking, not Cygnus failing: a client
# that went away leaves a ConnectionResetError from _call_connection_lost
# (28 of 141 problems in the log up to 2026-09-29, all harmless).
_BENIGN_EXC_RE = re.compile(r"^ConnectionResetError: \[WinError 10054\]")

# Lines that say nothing about what happened: the HUD's polling and sockets.
NOISE_RE = re.compile(
    r'"(?:GET|POST|HEAD) /(?:status|music/now|running|jobs|apps|vendor|favicon)[^"]*HTTP|'
    r'WebSocket /ws|^INFO:\s+connection (?:open|closed)|^\[WS\] client (?:dis)?connected|'
    r'Fetching \d+ files')


class LogScanner:
    """Feeds on backend log lines; returns the problems they finish. A
    traceback is one problem, reported when the exception line after it ends."""

    def __init__(self):
        self._tb: list[str] | None = None

    def feed(self, line: str) -> list[dict]:
        line = line.rstrip("\r\n")
        out = []
        if self._tb is not None:
            if line.startswith((" ", "\t")) or not line.strip() or \
                    line.startswith(("During handling", "The above exception")):
                self._tb.append(line)
                return out
            # The first line at column 0 after the frames is the exception.
            self._tb.append(line)
            if not _BENIGN_EXC_RE.search(line):
                out.append({"kind": "traceback", "severity": "error", "detail": line[:300],
                            "traceback": self._tb[-30:]})
            self._tb = None
            return out
        if line.startswith(_TRACEBACK):
            self._tb = [line]
            return out
        for rx, kind, severity in _LINE_RULES:
            if rx.search(line):
                out.append({"kind": kind, "severity": severity, "detail": line[:300]})
                break
        return out


def fingerprint(problem: dict) -> str:
    """The same problem again, whatever its numbers: repeats are counted, not
    announced each time."""
    detail = re.sub(r"\d+", "#", str(problem.get("detail", "")))[:120]
    return f"{problem.get('kind')}|{detail}"
