"""claude_prompt.py - type a prompt into a Claude Code session that is already
open, and say what Claude answered.

The owner's work is mostly prompting Claude Code in Windows Terminal. "Tell
Claude in my-app to run the tests" finds that session (running.py knows each
Claude Code process's folder, title and transcript), reads the prompt back,
and sends it a few seconds later unless the owner says "cancel". Claude's
reply is read from its own transcript, not scraped from the screen, and a
short summary is spoken when it is done.

Safety: the text is only ever typed into a Windows Terminal window whose title
is that session's title (Claude Code names its tab after the session), checked
before the paste and again before Enter, so a prompt can never land in a bare
shell and run as a command. Claude Code's own permission prompts still apply.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time

from cygnus.apps import running as running_apps

WT_CLASS = "CASCADIA_HOSTING_WINDOW_CLASS"
REPLY_TIMEOUT_S = 20 * 60          # Claude can work a long while
POLL_S = 2.0
SUMMARY_CHARS = 240


class PromptError(Exception):
    """Why a prompt could not be sent, in words for the owner."""


# ---------------------------------------------------------------- sessions --

def _norm(title: str) -> str:
    """'◐ Portfolio design overhaul' -> 'portfolio design overhaul': Claude Code
    puts a spinner or a star before its title."""
    return re.sub(r"^[^\w]+", "", title or "").strip().lower()


def sessions(rows=None) -> list[dict]:
    """The open Claude Code sessions: name, folder, title, transcript, status."""
    rows = running_apps.inventory(use_cache=False) if rows is None else rows
    out = []
    for r in rows:
        s = r.get("session")
        if s and s.get("transcript"):
            out.append({"name": r["name"], "folder": s.get("folder") or "", "title": s.get("title") or "",
                        "transcript": s["transcript"], "status": r.get("status", "")})
    return out


def pick(name: str | None, rows=None) -> dict:
    """The session the owner named ("my-app", "portfolio", "Cygnus"), or the
    only one open. Raises PromptError when none or several fit."""
    ss = sessions(rows)
    if not ss:
        raise PromptError("no Claude Code session is open")
    names = ", ".join(s["name"] for s in ss)
    want = re.sub(r"\b(?:claude(?:\s+code)?|in|the|session|project|folder)\b", " ", (name or "").lower())
    want = " ".join(want.split())
    if not want:
        if len(ss) == 1:
            return ss[0]
        raise PromptError(f"which Claude Code session? Open now: {names}")

    def score(s):
        hay = f"{s['folder']} {s['title']}".lower().replace("-", " ")
        if want.replace("-", " ") in hay:
            return 1.0
        return max(difflib.SequenceMatcher(None, want, w).ratio() for w in hay.split() + [hay])
    best = max(ss, key=score)
    if score(best) < 0.6:
        raise PromptError(f"no Claude Code session called “{name}”; open now: {names}")
    return best


# ------------------------------------------------------------------ window --

def _wt_windows() -> list[tuple[int, str]]:
    """(hwnd, title) of every Windows Terminal window."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == WT_CLASS and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append((int(hwnd), buf.value))
        return True
    user32.EnumWindows(each, 0)
    return found


def _title_of(hwnd: int) -> str:
    import ctypes
    user32 = ctypes.windll.user32
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _front(hwnd: int, title: str) -> bool:
    """The session's window is in front and still shows the session."""
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow() == hwnd and _norm(_title_of(hwnd)) == title


def _find_tab(hwnd: int, title: str):
    """The tab of a Windows Terminal window whose name is the session, or None."""
    try:
        from pywinauto import Desktop
        win = Desktop(backend="uia").window(handle=hwnd)
        for tab in win.descendants(control_type="TabItem"):
            if _norm(tab.element_info.name) == title:
                return tab
    except Exception:
        pass
    return None


def _bring(session: dict) -> int:
    """Bring the session's terminal window (and tab) to the front; its hwnd."""
    title = _norm(session["title"])
    if not title:
        raise PromptError(f"{session['name']} has no title yet, so I can't tell which terminal it is in")
    wins = _wt_windows()
    hwnd = next((h for h, t in wins if _norm(t) == title), None)
    tab = None
    if hwnd is None:
        for h, _ in wins:
            tab = _find_tab(h, title)
            if tab is not None:
                hwnd = h
                break
    if hwnd is None:
        raise PromptError(f"I can't find the terminal window of {session['name']}")
    for _ in range(2):
        running_apps.bring_to_front({"name": session["name"], "windows": [{"hwnd": hwnd}]})
        if tab is not None:
            try:
                tab.select()
            except Exception:
                pass
        end = time.time() + 2.5
        while time.time() < end:
            if _front(hwnd, title):
                time.sleep(0.3)                     # let a restore finish before any key
                return hwnd
            time.sleep(0.1)
    raise PromptError(f"Windows wouldn't bring {session['name']} to the front")


def _type(session: dict, text: str) -> None:
    """Paste the prompt into the session and press Enter, checking before each."""
    from pywinauto import keyboard
    from cygnus.apps.commands import _clipboard
    title = _norm(session["title"])
    hwnd = _bring(session)
    if not _front(hwnd, title):
        raise PromptError("the focus moved away, so I stopped before typing")
    with _clipboard(text):
        keyboard.send_keys("^v")
        time.sleep(0.4)
    if not _front(hwnd, title):
        raise PromptError("the focus moved away after pasting, so I didn't press Enter; "
                          f"the prompt is waiting in {session['name']}")
    keyboard.send_keys("{ENTER}")


# ------------------------------------------------------------------- reply --

def _records(path: str) -> list[dict]:
    out = []
    for line in running_apps._tail(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _when(r: dict) -> float:
    return running_apps._stamp(r.get("timestamp")) or 0.0


def reply_since(path: str, sent_at: float) -> str | None:
    """Claude's answer to a prompt sent at `sent_at`, once it has finished:
    the text of its last message after that prompt, with no tool call left
    pending. None while it is still working."""
    recs = [r for r in _records(path) if _when(r) >= sent_at - 5]
    if not any(r.get("type") == "user" for r in recs):
        return None                                  # the prompt isn't in the transcript yet
    last_text, pending = None, False
    for r in recs:
        content = (r.get("message") or {}).get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if r.get("type") == "assistant":
            if any(b.get("type") == "tool_use" for b in blocks):
                pending = True
            texts = [b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text", "").strip()]
            if texts and not any(b.get("type") == "tool_use" for b in blocks):
                last_text, pending = "\n".join(texts), False
        elif r.get("type") == "user" and any(b.get("type") == "tool_result" for b in blocks):
            pending = True
    return None if pending or not last_text else last_text


def summary(text: str, limit: int = SUMMARY_CHARS) -> str:
    """The first sentences of Claude's reply, as speech: no markdown, no code."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"^\s*\|.*\|\s*$", " ", t, flags=re.M)          # tables
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s*(?:#+|[-*]|\d+\.)\s+", "", t, flags=re.M)
    t = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", t)
    t = " ".join(t.split())
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", t):
        if out and len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}".strip()
        if len(out) >= limit * 0.6:
            break
    return out[:limit].rstrip() or t[:limit]


# -------------------------------------------------------- send, with cancel --

_lock = threading.Lock()
_pending: dict | None = None


def delay_for(text: str) -> float:
    """Seconds between the read-back and the send: time to hear it, plus about
    three to say "Cygnus, cancel"."""
    return min(25.0, 6.0 + 0.45 * len(text.split()))


def schedule(session: dict, text: str, on_done=None, delay: float | None = None, send=None) -> str:
    """Send `text` to `session` after the read-back delay unless cancelled;
    tell on_done what Claude answered. What to say now."""
    global _pending
    text = " ".join((text or "").split())
    if not text:
        raise PromptError("there's nothing to send")
    cancelled = threading.Event()
    job = {"session": session, "text": text, "cancelled": cancelled}
    with _lock:
        if _pending:
            _pending["cancelled"].set()             # a newer prompt replaces a waiting one
        _pending = job
    wait = delay_for(text) if delay is None else delay
    send = send or (lambda s, t: running_apps.ui_call(_type, s, t))

    def work():
        global _pending
        if cancelled.wait(wait):
            return
        with _lock:
            if _pending is job:
                _pending = None
        sent_at = time.time()
        try:
            send(session, text)
        except PromptError as e:
            _tell(on_done, f"I didn't send it to {session['name']}: {e}, sir.")
            return
        except Exception as e:
            _tell(on_done, f"I couldn't type into {session['name']} ({type(e).__name__}), sir.")
            return
        end = time.time() + REPLY_TIMEOUT_S
        while time.time() < end:
            time.sleep(POLL_S)
            answer = reply_since(session["transcript"], sent_at)
            if answer:
                _tell(on_done, f"Claude in {session['folder'] or session['name']}: {summary(answer)}")
                return
        _tell(on_done, f"{session['name']} is still working after twenty minutes, sir.")

    threading.Thread(target=work, daemon=True, name="claude-prompt").start()
    return (f"Sending to {session['name']}: “{text}”. Say “cancel” to stop it, sir.")


def _tell(on_done, text: str) -> None:
    """Say it when it lands; one text for the HUD and the voice, so Claude's
    full answer stays in its terminal."""
    print(f"[claude_prompt] {ascii(text[:160])}", flush=True)
    if on_done:
        try:
            on_done(text)
        except Exception as e:
            print(f"[claude_prompt] on_done failed: {e!r}", flush=True)


def decline_pending() -> str | None:
    """The owner's "cancel" while a prompt waits to be sent; None if none waits."""
    global _pending
    with _lock:
        job, _pending = _pending, None
    if not job:
        return None
    job["cancelled"].set()
    return f"Cancelled. Nothing was sent to {job['session']['name']}, sir."


def pending() -> dict | None:
    with _lock:
        return dict(_pending) if _pending else None
