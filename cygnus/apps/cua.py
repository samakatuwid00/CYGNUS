"""Cua Driver (trycua) as Cygnus's hands inside a window: open a chat, a tab
or a list entry by its name.

The driver is a separate program (cua-driver.exe, a daemon on a named pipe)
that reads a window's UI Automation tree and acts on its elements in the
background: no cursor move, no focus theft. Cygnus runs its command line,
`cua-driver call <tool> <json>`, one call per step (about a second each).
An element found by one call can be acted on by the next: the daemon keeps
the snapshot.

What never leaves this module: the window's text. Telegram's tree holds its
messages as list items too (labels up to ~3000 characters, previews on the
chat entries), so the names a window shows are matched here, against what
the owner said, and only the one that was opened is reported. No screenshot
is taken.

Missing driver, stopped daemon, a window it cannot read: available() is
False or DriveError explains it, and Cygnus carries on without it.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

CALL_TIMEOUT_S = 30
STATUS_TTL_S = 60            # how long a daemon check is trusted
MAX_ELEMENTS = 4000          # one window's tree; Chrome runs to ~5000
MAX_WINDOWS = 4              # windows of one program searched for a name
VERIFY_WAIT_S = 2.5          # for the window title to name what was opened
CLICK_SETTLE_S = 0.8         # between a text click and reading the window again
ITEM_ROLES = ("ListItem", "TabItem", "TreeItem")
ITEM_ACTIONS = ("invoke", "select")

_status = {"at": 0.0, "ok": False}
_status_lock = threading.Lock()


class CuaError(Exception):
    """The driver could not do what was asked; the message says why."""


def exe() -> str | None:
    """cua-driver.exe: $CYGNUS_CUA_DRIVER, the installer's location, or PATH."""
    for cand in (os.getenv("CYGNUS_CUA_DRIVER"),
                 str(Path.home() / ".cua-driver" / "packages" / "current" / "cua-driver.exe"),
                 shutil.which("cua-driver")):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _run(args: list[str], timeout: float = CALL_TIMEOUT_S) -> subprocess.CompletedProcess:
    path = exe()
    if not path:
        raise CuaError("the Cua driver isn't installed")
    try:
        return subprocess.run([path, *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise CuaError(f"the Cua driver took longer than {int(timeout)} s")
    except OSError as e:
        raise CuaError(f"the Cua driver would not start ({e})")


def available() -> bool:
    """The driver is installed and its daemon answers. Checked at most once a
    minute: every app_actions call asks. (Tests stub this; _daemon_up is the
    check itself.)"""
    return _daemon_up()


def _daemon_up() -> bool:
    with _status_lock:
        if time.monotonic() - _status["at"] < STATUS_TTL_S:
            return _status["ok"]
        ok = False
        if exe():
            try:
                ok = "daemon is running" in (_run(["status"], timeout=10).stdout or "")
            except CuaError:
                ok = False
        _status.update(at=time.monotonic(), ok=ok)
        return ok


def call(tool: str, args: dict, timeout: float = CALL_TIMEOUT_S) -> dict:
    """One driver tool call; its JSON answer. CuaError when it failed."""
    proc = _run(["call", tool, json.dumps(args)], timeout=timeout)
    out = proc.stdout or ""
    start = out.find("{")
    try:
        data = json.loads(out[start:]) if start >= 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # A failed action can still exit 0 with a line of text instead of
        # JSON. On Windows Settings (2026-09-25) a pixel click printed "The
        # operation completed successfully. (0x00000000)" and clicked nothing,
        # and its foreground retry printed "foreground_unavailable: ...".
        text = (out or proc.stderr or "").strip()
        why = text.splitlines()[-1] if text else "no answer"
        if "operation completed successfully" in why.lower():
            why = "Windows refused the input for that window"
        elif why.startswith("foreground_unavailable"):
            why = "Windows would not bring that window forward, so nothing was clicked"
        raise CuaError(f"the Cua driver's {tool} failed: {why[:200]}")
    if data.get("isError") or data.get("error"):
        err = data.get("error") or data.get("message") or data.get("content") or "an error"
        raise CuaError(f"the Cua driver's {tool} failed: {str(err)[:200]}")
    return data


def window_pid(hwnd: int) -> int:
    """The process that owns a window: the driver wants pid and window_id to agree."""
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value)


# ----------------------------------------------------------------- names --
#
# Measured on Telegram Desktop (2026-09-25): its tree has two lists. "Chats"
# holds one single-line item per chat, the name first and then the last
# message's preview and time, up to ~180 characters. "Messages" holds the
# messages, ten lines each. So an item is judged by the list it sits in, a
# name is matched only at the start of an item, and what is said back is the
# owner's own name for it, never the item's text.

_STOP = {"the", "a", "an", "my", "our", "tab", "tabs", "chat", "chats", "conversation", "group",
         "channel", "window", "session", "item", "entry", "in", "on", "at", "to", "with", "one"}
# A list whose items are content, not places to go.
_CONTENT_LIST_RE = re.compile(r"\b(messages?|conversation|history|comments?|replies|thread|"
                              r"transcript|log|output|posts?|feed)\b", re.I)
CONTAINER_ROLES = ("List", "Tab", "Tree", "DataGrid", "Table")
NAME_SLACK = 3               # words a name may run past what was said ("cerebrum (eur/usd)")
MAX_LABEL = 300


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def item_text(label: str) -> str:
    """An item's first line, without the invisible marks Telegram adds."""
    lines = (label or "").strip().splitlines()
    first = re.sub("[\u200b-\u200f\u2060\ufeff]", "", lines[0] if lines else "").strip()
    return first[:MAX_LABEL]


def _span(want: set[str], text: str, anywhere: bool = False):
    """exact (True/False) when `text` names every wanted word, else None.

    A tab's title is all name, so the words may be anywhere in it. Anything
    else must START with the spoken words and stay within a few words:
    "cerebrum" matches "cerebrum (eur/usd)", but not "sent the chart about
    cerebrum". A leading unread count is skipped.
    """
    toks = _tokens(text)
    while toks and toks[0].isdigit():
        toks = toks[1:]
    pos = []
    for w in want:
        if w not in toks:
            return None
        pos.append(toks.index(w))
    if not anywhere and (min(pos) != 0 or max(pos) >= len(want) + NAME_SLACK):
        return None
    return [t for t in toks if t not in _STOP] == sorted(want, key=toks.index)


def name_fields(text: str, anywhere: bool = False) -> list[str]:
    """Where an item's name can be. A tab's whole title. A list item's first
    two comma-separated fields: Telegram labels a chat "<field>, <field>,
    <preview>, ..." (measured 2026-09-25), and which of the first two holds
    the chat's name was not settled without reading the owner's messages.
    The preview, from the third field on, is never matched, and neither is a
    field with a colon in it: "Chloe: backtest photo" is a sender and a
    message, not a chat called Chloe."""
    if anywhere:
        return [text]
    return [f.strip() for f in text.split(",")[:2] if f.strip() and ":" not in f]


def match(name: str, texts: list[str], anywhere: list[bool] | None = None) -> tuple[list[int], bool]:
    """Which items `name` means: (their positions in `texts`, exact).

    Exact matches (a field whose words are just the spoken ones) win; then
    matches in an item's first field; then in its second. Everything that
    ties is returned, so the caller can ask instead of guessing.
    `anywhere[i]` makes item i a tab: its whole title is the name.
    """
    want = set(_tokens(name)) - _STOP
    if not want:
        return [], False
    anywhere = anywhere or [False] * len(texts)
    hits = []                                        # (item, exact, field)
    for i, text in enumerate(texts):
        best = None
        for f, field in enumerate(name_fields(text, anywhere[i])):
            exact = _span(want, field, anywhere[i])
            if exact is not None and (best is None or (not exact, f) < (not best[0], best[1])):
                best = (exact, f)
        if best is not None:
            hits.append((i, *best))
    if not hits:
        return [], False
    top = min((not exact, f) for _, exact, f in hits)
    return [i for i, exact, f in hits if (not exact, f) == top], not top[0]


def window_title(hwnd: int) -> str:
    """A window's title as Windows has it now, invisible marks removed."""
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(int(hwnd), buf, 512)
    return item_text(buf.value)


def shows(hwnd: int, name: str, wait: float = VERIFY_WAIT_S) -> bool:
    """Whether the window's title names what was opened. Telegram titles its
    window with the open chat, Windows Terminal with the active tab, so this
    checks the click did what was asked, whatever the list's labels were."""
    want = set(_tokens(name)) - _STOP
    end = time.monotonic() + wait
    while True:
        if want and want <= set(_tokens(window_title(hwnd))):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.25)


def shown_name(name: str, role: str, text: str) -> str:
    """What to call the item when saying it was opened. A tab's title is its
    name; a list item's text runs on into a preview, so it is called what the
    owner called it."""
    if role in ("TabItem", "TreeItem"):
        return text[:80]
    return (name or "").strip()


# ------------------------------------------------------------- the driver --

def _items(pid: int, hwnd: int, content: list | None = None) -> list[dict]:
    """A window's entries that can be opened: {text, role, token}. Items of a
    content list (Telegram's "Messages") are left out, and `content`, when
    given, gets a True for each one found: such a window holds private text,
    and is never read by OCR. No screenshot."""
    data = call("get_window_state", {"pid": pid, "window_id": int(hwnd), "include_screenshot": False,
                                     "max_elements": MAX_ELEMENTS})
    elements = data.get("elements") or []
    by_index = {e.get("element_index"): e for e in elements}

    def in_content_list(e) -> bool:
        seen = 0
        parent = by_index.get(e.get("parent_index"))
        while parent is not None and seen < 40:
            if parent.get("role") in CONTAINER_ROLES:
                return bool(_CONTENT_LIST_RE.search(parent.get("label") or ""))
            parent = by_index.get(parent.get("parent_index"))
            seen += 1
        return False

    out = []
    for e in elements:
        if e.get("role") not in ITEM_ROLES or not e.get("enabled", True):
            continue
        if not set(e.get("actions") or ()) & set(ITEM_ACTIONS) or not e.get("element_token"):
            continue
        text = item_text(e.get("label") or "")
        if not text:
            continue
        if in_content_list(e):
            if content is not None:
                content.append(True)
            continue
        out.append({"text": text, "role": e["role"], "token": e["element_token"]})
    return out


class CuaWindow:
    """Any program with a window, driven through the Cua driver."""

    def __init__(self, row: dict):
        self.row = row
        self.label = row.get("name") or "that app"
        self.windows = [w["hwnd"] for w in (row.get("windows") or []) if w.get("hwnd")][:MAX_WINDOWS]

    def open_item(self, name: str, show: bool = True) -> str:
        """Open the chat, tab or list entry the owner named, then bring its
        window forward so they see it."""
        from cygnus.apps.commands import DriveError
        if not self.windows:
            raise DriveError(f"{self.label} has no window I can reach")
        found = []                                   # {text, role, token, pid, hwnd}
        private = []                                 # a content list was seen
        try:
            for hwnd in self.windows:
                pid = window_pid(hwnd)
                found += [dict(i, pid=pid, hwnd=hwnd) for i in _items(pid, hwnd, private)]
        except CuaError as e:
            raise DriveError(str(e))
        picks, _exact = match(name, [f["text"] for f in found],
                              [f["role"] in ("TabItem", "TreeItem") for f in found])
        if not picks:
            # The accessibility tree has no such entry. Read the window's text
            # on this PC instead, unless the window holds messages: those are
            # never read, matched or clicked.
            from cygnus.apps import screen_text
            if not private and screen_text.available():
                return self.click_text(name, show=show)
            # Nothing the window shows is read out: it may be message text.
            raise DriveError(f"I couldn't find anything called “{name}” in {self.label}")
        if len({found[i]["text"] for i in picks}) > 1:
            if all(found[i]["role"] == "TabItem" for i in picks):
                options = " or ".join(f"“{found[i]['text'][:60]}”" for i in picks[:3])
                raise DriveError(f"more than one tab in {self.label} matches “{name}”: {options}. "
                                 "Say which one")
            # A list item's text carries a preview: count them, don't read them.
            raise DriveError(f"{len(picks)} entries in {self.label} match “{name}”. "
                             "Say more of the name")
        pick = found[picks[0]]
        try:
            call("click", {"pid": pick["pid"], "window_id": int(pick["hwnd"]), "element_token": pick["token"]})
            seen = shows(pick["hwnd"], name)
            if show:
                call("bring_to_front", {"pid": pick["pid"], "window_id": int(pick["hwnd"])})
        except CuaError as e:
            raise DriveError(str(e))
        called = shown_name(name, pick["role"], pick["text"])
        if not seen:
            # The click landed, but the window does not name what was asked:
            # say so rather than claim it (the title is what the owner sees).
            return (f"I selected something in {self.label} for “{name}”, but its window now "
                    f"shows “{window_title(pick['hwnd'])[:80]}”, so it may not be the one you meant.")
        if pick["role"] == "TabItem":
            return f"Opened the tab “{called}” in {self.label}."
        return f"Opened “{called}” in {self.label}."

    def click_text(self, name: str, show: bool = True) -> str:
        """Click the words the owner said where they appear in the window,
        read by OCR on this PC (screen_text), then read the window again to
        see whether anything happened. Only the owner's own words are said
        back; the rest of the window's text stays here."""
        from cygnus.apps import screen_text
        from cygnus.apps.commands import DriveError
        hits, where = [], None
        try:
            for hwnd in self.windows:
                pid = window_pid(hwnd)
                before = screen_text.read(pid, hwnd)
                hits = screen_text.find(before, name)
                if hits:
                    where = (pid, hwnd, before)
                    break
        except (CuaError, screen_text.OcrError) as e:
            raise DriveError(str(e))
        except Exception as e:                       # the OCR engine itself
            raise DriveError(f"I couldn't read {self.label}'s window ({type(e).__name__})")
        if not hits:
            raise DriveError(f"I couldn't find “{name}” in {self.label}, "
                             "neither in its lists nor in the text on its window")
        if len(hits) > 1:
            raise DriveError(f"“{name}” appears {len(hits)} times in {self.label}'s window. "
                             "Say more of it")
        pid, hwnd, before = where
        hit = hits[0]
        try:
            _click_at(pid, hwnd, hit["x"], hit["y"])
            after = before
            end = time.monotonic() + VERIFY_WAIT_S
            while True:
                time.sleep(CLICK_SETTLE_S)
                after = screen_text.read(pid, hwnd)
                if screen_text.changed(before, after) or time.monotonic() >= end:
                    break
            if show:
                call("bring_to_front", {"pid": pid, "window_id": int(hwnd)})
        except (CuaError, screen_text.OcrError) as e:
            raise DriveError(str(e))
        if screen_text.changed(before, after):
            return f"Clicked “{hit['text']}” in {self.label}, and the window changed."
        return (f"I clicked “{hit['text']}” in {self.label}, but nothing on its window changed, "
                "so it may not have worked.")


def _click_at(pid: int, hwnd: int, x: int, y: int) -> None:
    """A click at screenshot pixels, in the background first. Chromium pages
    drop background clicks; the driver says so (background_unavailable), and
    only then is the click repeated in the foreground, as its docs require."""
    args = {"pid": pid, "window_id": int(hwnd), "x": int(x), "y": int(y)}
    try:
        call("click", args)
    except CuaError as e:
        if "background_unavailable" not in str(e):
            raise
        call("click", dict(args, delivery_mode="foreground"))
