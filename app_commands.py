"""Slash commands for what is running (APP_COMMANDS_PLAN.md).

"/hermes start a new session then prompt: what is the status of second brain"
is carried out in the Hermes that is open, the way it runs. Version 1 drives
the Hermes desktop app through its window: raise it, Ctrl+N for a new session,
paste into its Message box, Enter, read the reply back. Other running programs
are listed for the "/" menu and marked not drivable yet.

Every key goes only to the target: the foreground window is checked before
each one, because a key sent after focus moved lands in whatever has focus
(the owner's app, or a Claude Code terminal).
"""

from __future__ import annotations

import ctypes
import re
import threading
import time
from contextlib import contextmanager

import running_apps

READ_TIMEOUT_S = 240          # a Hermes turn with tools can take minutes
SETTLE_S = 3.0                # the reply counts as finished once it stops growing this long
_run_lock = threading.Lock()

_NEW_SESSION_RE = re.compile(r"^(?:please\s+)?(?:(?:start|open|begin|create|make)\s+)?(?:a\s+)?(?:new|fresh)\s+"
                             r"(?:session|chat|conversation|thread)$", re.I)
_SPLIT_RE = re.compile(r"\s*(?:,\s*)?\b(?:and\s+)?then\b\s*,?\s*|\s*;\s*", re.I)
_GENERIC = {"microsoft", "google", "windows", "desktop", "app", "the"}   # "Windows Terminal" is /terminal


class DriveError(Exception):
    """A step could not be carried out; nothing after it runs."""


# ------------------------------------------------------------------ steps --

def _split(text: str) -> list[str]:
    """Clauses joined by "then" or ";", never splitting inside double quotes."""
    parts, quoted = [], re.split(r'("[^"]*")', text or "")
    current = ""
    for i, chunk in enumerate(quoted):
        if i % 2:                                   # a quoted stretch stays whole
            current += chunk
            continue
        pieces = _SPLIT_RE.split(chunk)
        current += pieces[0]
        for piece in pieces[1:]:
            parts.append(current)
            current = piece
    parts.append(current)
    return [p.strip(" ,.") for p in parts if p.strip(" ,.")]


def _message(clause: str) -> str:
    """What to send for one clause: "prompt: X" is X, "ask hermes to X" is X,
    anything else is sent as it was said."""
    for pattern in (r"^(?:prompt|send|type|write)\b\s*[:,]?\s*(.+)$",
                    r"^(?:ask|tell)\s+(?:it|hermes|him|her|them|the agent)\s+(?:to\s+)?(.+)$",
                    r"^(?:ask|tell|say)\s*[:,]\s*(.+)$"):
        m = re.match(pattern, clause, re.I | re.S)
        if m:
            return m.group(1).strip().strip('"').strip()
    return clause.strip().strip('"').strip()


def plan(text: str) -> list[dict]:
    """The steps one instruction asks of an app, in order."""
    steps = []
    for clause in _split(text):
        if _NEW_SESSION_RE.match(clause):
            steps.append({"op": "new_session"})
        elif _message(clause):
            steps.append({"op": "send", "text": _message(clause)})
    return steps


def describe(step: dict) -> str:
    return "New session" if step["op"] == "new_session" else f'Send: "{step["text"]}"'


# ---------------------------------------------------------------- targets --

def _slug(name: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _GENERIC]
    return words[0] if words else ""


def _driver_for(row: dict) -> str | None:
    return "hermes-window" if row["name"] == "Hermes desktop app" else None


def targets(rows=None) -> list[dict]:
    """One "/" entry per running program with a window: drivable ones first.
    Two programs with the same short name get -2, -3."""
    rows = running_apps.inventory() if rows is None else rows
    out, used = [], {}
    for r in rows:
        if not r.get("windows") or r["group"] not in ("apps", "agents") or r["name"] == "Cygnus":
            continue
        driver = _driver_for(r)
        base = "hermes" if driver == "hermes-window" else _slug(r["name"])
        if not base:
            continue
        used[base] = used.get(base, 0) + 1
        out.append({"slug": base if used[base] == 1 else f"{base}-{used[base]}", "row": r["id"],
                    "name": r["name"], "doing": r.get("doing", ""), "kind": "window", "driver": driver})
    return sorted(out, key=lambda t: (t["driver"] is None, t["slug"]))


# ---------------------------------------------------------------- drivers --

@contextmanager
def _clipboard(text: str):
    """Put text on the clipboard for one paste, then put back what was there."""
    import win32clipboard as cb
    import win32con
    cb.OpenClipboard()
    try:
        saved = cb.GetClipboardData(win32con.CF_UNICODETEXT) if cb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT) else None
        cb.EmptyClipboard()
        cb.SetClipboardText(text, win32con.CF_UNICODETEXT)
    finally:
        cb.CloseClipboard()
    try:
        yield
    finally:
        time.sleep(0.2)                             # let the paste read it first
        cb.OpenClipboard()
        try:
            cb.EmptyClipboard()
            if saved is not None:
                cb.SetClipboardText(saved, win32con.CF_UNICODETEXT)
        finally:
            cb.CloseClipboard()


class HermesWindow:
    """The Hermes desktop app (Electron). Its controls appear to UI Automation
    only after a first query, so the Message box is looked for more than once.
    New session is Ctrl+N (Hermes keybinds, session.new = mod+n)."""

    label = "Hermes"

    def __init__(self, row: dict):
        self.row = row
        self.hwnd = row["windows"][0]["hwnd"]

    def _front(self) -> bool:
        return ctypes.windll.user32.GetForegroundWindow() == self.hwnd

    def _raise(self):
        running_apps.bring_to_front(self.row)
        time.sleep(0.4)
        if not self._front():
            raise DriveError("Hermes would not come to the front")

    def _keys(self, keys: str):
        from pywinauto import keyboard
        if not self._front():
            raise DriveError("the focus left Hermes, so I stopped before typing")
        keyboard.send_keys(keys)

    def _window(self):
        from pywinauto import Desktop
        return Desktop(backend="uia").window(handle=self.hwnd)

    def _message_box(self, win):
        for _ in range(5):
            boxes = [c for c in win.descendants(control_type="Edit") if (c.element_info.name or "") == "Message"]
            if boxes:
                return boxes[0]
            time.sleep(1)
        raise DriveError("Hermes's message box did not show")

    @staticmethod
    def _texts(win, above: int) -> list[str]:
        """The conversation's text: everything above the message box (the
        status bar under it, "7 t/s | RAM 14G/15G", is left out)."""
        out = []
        for c in win.descendants(control_type="Text"):
            info = c.element_info
            if info.name and info.rectangle.bottom <= above:
                out.append(info.name)
        return out

    def new_session(self) -> str:
        self._raise()
        self._keys("^n")
        time.sleep(1.5)
        return "Started a new Hermes session."

    def send(self, text: str) -> str:
        self._raise()
        win = self._window()
        box = self._message_box(win)
        top = box.element_info.rectangle.top
        before = set(self._texts(win, top))
        box.set_focus()
        time.sleep(0.3)
        with _clipboard(text):
            self._keys("^v")
            time.sleep(0.3)
        self._keys("{ENTER}")
        return self._reply(win, top, before, text)

    def _reply(self, win, top: int, before: set, sent: str) -> str:
        start, last, steady_since = time.time(), None, None
        while time.time() - start < READ_TIMEOUT_S:
            time.sleep(1.0)
            new = [t for t in self._texts(win, top) if t not in before and t.strip() != sent.strip()]
            joined = "\n".join(new).strip()
            if joined and joined == last:
                steady_since = steady_since or time.time()
                if time.time() - steady_since >= SETTLE_S:
                    return joined
            else:
                steady_since = None
            last = joined
        return (last or "") + ("\n(still going when I stopped reading)" if last else "")


DRIVERS = {"hermes-window": HermesWindow}


# -------------------------------------------------------------------- run --

def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def run(slug: str, steps: list[dict], rows=None) -> str:
    """Carry out the steps in the running app `slug`; what to tell the user."""
    if not _run_lock.acquire(blocking=False):
        return "Another app command is still running, sir. I'll take this one when it's done."
    try:
        try:
            import comtypes
            comtypes.CoInitialize()                 # UI Automation from a worker thread
        except Exception:
            pass
        rows = running_apps.inventory(use_cache=False) if rows is None else rows
        target = next((t for t in targets(rows) if t["slug"] == slug), None)
        if not target:
            return f"Nothing called /{slug} is running, sir."
        if not target["driver"]:
            return f"I can't drive {target['name']} yet, sir. Hermes is the first app I can use this way."
        row = next(r for r in rows if r["id"] == target["row"])
        driver = DRIVERS[target["driver"]](row)
        done = []
        for step in steps:
            try:
                if step["op"] == "new_session":
                    done.append(driver.new_session())
                else:
                    reply = driver.send(step["text"])
                    done.append(f'{driver.label}, to "{_short(step["text"])}": {reply or "(no reply I could read)"}')
            except DriveError as e:
                done.append(f"I stopped: {e}.")
                break
        return "\n".join(done)
    finally:
        _run_lock.release()
