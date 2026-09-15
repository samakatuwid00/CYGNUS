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
_run_lock = threading.Lock()

_NEW_SESSION_RE = re.compile(r"^(?:please\s+)?(?:(?:start|open|begin|create|make)\s+)?(?:a\s+)?(?:new|fresh)\s+"
                             r"(?:session|chat|conversation|thread)$", re.I)
# "new session" anywhere a person says it, not only as its own "then" step:
# "/hermes on the new session prompt what is the status of musubae" pasted the
# whole line into the open session (2026-09-15).
_SESSION = r"(?:a\s+|the\s+)?(?:new|fresh)\s+(?:session|chat|conversation|thread)"
_NEW_SESSION_LEAD_RE = re.compile(r"^(?:please\s+)?(?:(?:on|in|with|using|start|open|begin|create|make)\s+)?"
                                  + _SESSION + r"\b\s*(?:[,:;-]\s*|\s(?:and\s+)?(?:then\s+)?)", re.I)
_NEW_SESSION_TAIL_RE = re.compile(r"\s*,?\s+(?:on|in)\s+" + _SESSION + r"\s*[.!]?$", re.I)
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
    text = (text or "").strip()
    lead = _NEW_SESSION_LEAD_RE.match(text)
    if lead and text[lead.end():].strip():
        return [{"op": "new_session"}] + [s for s in plan(text[lead.end():]) if s["op"] != "new_session"]
    tail = _NEW_SESSION_TAIL_RE.search(text)
    if tail and text[:tail.start()].strip():
        return [{"op": "new_session"}] + [s for s in plan(text[:tail.start()]) if s["op"] != "new_session"]
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

def _clip_get():
    import win32clipboard as cb
    import win32con
    cb.OpenClipboard()
    try:
        return cb.GetClipboardData(win32con.CF_UNICODETEXT) if cb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT) else None
    finally:
        cb.CloseClipboard()


def _clip_set(text):
    import win32clipboard as cb
    import win32con
    cb.OpenClipboard()
    try:
        cb.EmptyClipboard()
        if text is not None:
            cb.SetClipboardText(text, win32con.CF_UNICODETEXT)
    finally:
        cb.CloseClipboard()


@contextmanager
def _clipboard(text: str):
    """Put text on the clipboard for one paste, then put back what was there."""
    saved = _clip_get()
    _clip_set(text)
    try:
        yield
    finally:
        time.sleep(0.2)                             # let the paste read it first
        _clip_set(saved)


def _copied(action) -> str | None:
    """What `action` (pressing an app's Copy button) puts on the clipboard; the
    owner's clipboard is put back after."""
    saved = _clip_get()
    try:
        _clip_set("")
        action()
        time.sleep(0.8)
        return _clip_get()
    finally:
        _clip_set(saved)


def tidy_reply(text: str) -> str:
    """A Hermes reply as Cygnus says it: its file-preview marker
    (::preview{file="C:/.../x.html"}) becomes a plain sentence."""
    text = re.sub(r'::preview\{file="([^"]+)"\}',
                  lambda m: f"(Hermes made {m.group(1).replace(chr(92), '/').rsplit('/', 1)[-1]}; it's open in Hermes.)",
                  text or "")
    return text.strip()


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
        """Bring Hermes forward and wait until it really is. A minimised
        window takes a moment to restore: a single 0.4 s check gave up on a
        Hermes that was on its way up (2026-09-15)."""
        for _ in range(2):
            running_apps.bring_to_front(self.row)
            end = time.time() + 2.0
            while time.time() < end:
                if self._front():
                    time.sleep(0.3)                 # let the restore finish before any key
                    return
                time.sleep(0.1)
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

    @staticmethod
    def _buttons(win, name: str) -> list:
        return [c for c in win.descendants(control_type="Button") if (c.element_info.name or "") == name]

    def send(self, text: str) -> str:
        self._raise()
        win = self._window()
        box = self._message_box(win)
        top = box.element_info.rectangle.top
        before = set(self._texts(win, top))
        finished_before = len(self._buttons(win, "Read aloud"))
        box.set_focus()
        time.sleep(0.3)
        with _clipboard(text):
            self._keys("^v")
            time.sleep(0.3)
        self._keys("{ENTER}")
        return self._reply(win, top, before, finished_before, text)

    def _reply(self, win, top: int, before: set, finished_before: int, sent: str) -> str:
        """Hermes's own copy of its reply. A finished reply gets its own
        Copy / Read aloud / Refresh buttons, so one more "Read aloud" means it
        is done; its Copy gives the reply as written. Scraping the window's
        text instead took a code block apart token by token (2026-09-15)."""
        start = time.time()
        while time.time() - start < READ_TIMEOUT_S:
            time.sleep(1.5)
            if len(self._buttons(win, "Read aloud")) > finished_before:
                copies = self._buttons(win, "Copy")
                copied = _copied(copies[-1].invoke) if copies else None
                if copied:
                    return tidy_reply(copied)
                break
        new = [t for t in self._texts(win, top) if t not in before and t.strip() != sent.strip()]
        still = "" if time.time() - start < READ_TIMEOUT_S else "\n(still going when I stopped reading)"
        return "\n".join(new).strip() + still


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
