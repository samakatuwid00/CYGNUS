"""Slash commands for what is running (APP_COMMANDS_PLAN.md).

"/hermes go to my musubae chat and ask for an update" is carried out in the
Hermes that is open. The instruction is read by the model, not matched
against phrases: the model gets the app's own actions (Hermes: new session,
open a session by name, send; other apps: their abilities and the owner's
rules) and may only use those. What it cannot map to one of them is not
guessed: Cygnus says what it can do there and how to add a rule.

Every key goes only to the target: the foreground window is checked before
each one, because a key sent after focus moved lands in whatever has focus
(the owner's app, or a Claude Code terminal).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
from contextlib import contextmanager

from cygnus.apps import running as running_apps

READ_TIMEOUT_S = 240          # a Hermes turn with tools can take minutes
MAX_ACTIONS = 60              # what the model is shown for one app
_run_lock = threading.Lock()

_GENERIC = {"microsoft", "google", "windows", "desktop", "app", "the"}   # "Windows Terminal" is /terminal


class DriveError(Exception):
    """A step could not be carried out; nothing after it runs."""


# ---------------------------------------------- fallback phrase reading --
# Only for when no model answers: Hermes then still takes "new session" and
# "prompt: X" phrasings. Everything else goes through understand().

_NEW_SESSION_RE = re.compile(r"^(?:please\s+)?(?:(?:start|open|begin|create|make)\s+)?(?:a\s+)?(?:new|fresh)\s+"
                             r"(?:session|chat|conversation|thread)$", re.I)
_SESSION = r"(?:a\s+|the\s+)?(?:new|fresh)\s+(?:session|chat|conversation|thread)"
_NEW_SESSION_LEAD_RE = re.compile(r"^(?:please\s+)?(?:(?:on|in|with|using|start|open|begin|create|make)\s+)?"
                                  + _SESSION + r"\b\s*(?:[,:;-]\s*|\s(?:and\s+)?(?:then\s+)?)", re.I)
_NEW_SESSION_TAIL_RE = re.compile(r"\s*,?\s+(?:on|in)\s+" + _SESSION + r"\s*[.!]?$", re.I)
_SPLIT_RE = re.compile(r"\s*(?:,\s*)?\b(?:and\s+)?then\b\s*,?\s*|\s*;\s*", re.I)


def _split(text: str) -> list[str]:
    """Clauses joined by "then" or ";", never splitting inside double quotes."""
    parts, quoted = [], re.split(r'("[^"]*")', text or "")
    current = ""
    for i, chunk in enumerate(quoted):
        if i % 2:
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
    for pattern in (r"^(?:prompt|send|type|write)\b\s*[:,]?\s*(.+)$",
                    r"^(?:ask|tell)\s+(?:it|hermes|him|her|them|the agent)\s+(?:to\s+)?(.+)$",
                    r"^(?:ask|tell|say)\s*[:,]\s*(.+)$"):
        m = re.match(pattern, clause, re.I | re.S)
        if m:
            return m.group(1).strip().strip('"').strip()
    return clause.strip().strip('"').strip()


def plan(text: str) -> list[dict]:
    """Hermes steps from fixed phrasings: the fallback when no model answers."""
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
    op = step["op"]
    if op == "new_session":
        return "New session"
    if op == "open_session":
        return f'Open session "{step["name"]}"'
    if op == "send":
        return f'Send: "{step["text"]}"'
    detail = ", ".join(str(v) for v in (step.get("args") or {}).values() if str(v).strip())
    return step.get("label", step.get("id", op)) + (f": {detail}" if detail else "")


# ------------------------------------------------------------- targets --

def _apps() -> dict:
    try:
        from cygnus.apps import abilities as app_abilities
        return app_abilities.load_apps().get("apps") or {}
    except Exception:
        return {}


def _registry_keys(row: dict, apps: dict) -> list[str]:
    """The registry entries for this running program, matched by its program
    file (PowerPoint is both "powerpnt" and "powerpoint")."""
    exes = set(row.get("exes") or [])
    return [k for k, e in apps.items()
            if (e.get("exe") and str(e["exe"]).lower() in exes)
            or (e.get("bin") and os.path.basename(str(e["bin"])).lower() in exes)]


def _usable(ability: dict) -> bool:
    return ability.get("enabled", True) is not False and ability.get("level") != "never"


def _slug(name: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _GENERIC]
    return words[0] if words else ""


def _driver_for(row: dict, apps: dict) -> str | None:
    if row["name"] == "Hermes desktop app":
        return "hermes-window"
    for key in _registry_keys(row, apps):
        entry = apps[key]
        if any(_usable(a) for a in entry.get("abilities") or []) or entry.get("compiled_rules"):
            return "abilities"
    return None


def targets(rows=None, apps=None) -> list[dict]:
    """One "/" entry per running program with a window: drivable ones first.
    Two programs with the same short name get -2, -3."""
    rows = running_apps.inventory() if rows is None else rows
    apps = _apps() if apps is None else apps
    out, used = [], {}
    for r in rows:
        if not r.get("windows") or r["group"] not in ("apps", "agents") or r["name"] == "Cygnus":
            continue
        driver = _driver_for(r, apps)
        base = "hermes" if driver == "hermes-window" else _slug(r["name"])
        if not base:
            continue
        used[base] = used.get(base, 0) + 1
        out.append({"slug": base if used[base] == 1 else f"{base}-{used[base]}", "row": r["id"],
                    "name": r["name"], "doing": r.get("doing", ""), "kind": "window", "driver": driver})
    return sorted(out, key=lambda t: (t["driver"] is None, t["slug"]))


# ------------------------------------------------------------- actions --

def _rule_label(rule: dict) -> str:
    try:
        from cygnus.rules import voice as rules_voice
        return re.sub(r"^\s*\d+[.)]\s*", "", rules_voice._rule_line(1, rule)).strip()
    except Exception:
        return str(rule.get("trigger") or rule.get("rule_id") or "rule")


def actions(target: dict, row: dict, apps: dict, sessions=()) -> list[dict]:
    """What the model may use for this app: {"do", "does", "args", "choices"}."""
    if target["driver"] == "hermes-window":
        return [{"do": "new_session", "does": "start a new Hermes session"},
                {"do": "open_session", "does": "switch to an existing Hermes session by its title",
                 "args": {"name": "the session's title"}, "choices": list(sessions)[:20]},
                {"do": "send", "does": "send a message to Hermes in the current session and read its reply",
                 "args": {"text": "the message, in the user's words"}}]
    out = []
    for key in _registry_keys(row, apps):
        entry = apps[key]
        for a in entry.get("abilities") or []:
            if _usable(a):
                out.append({"do": f"ability:{key}:{a['id']}", "does": a.get("name") or a["id"],
                            "args": {k: str(v) for k, v in (a.get("needs") or {}).items()},
                            "ask": a.get("level") == "ask", "scanned": a.get("source") == "scan"})
        for rule in entry.get("compiled_rules") or []:
            if rule.get("rule_id"):
                out.append({"do": f"rule:{key}:{rule['rule_id']}", "does": f"your rule: {_rule_label(rule)}",
                            "args": {}})
    seen, unique = set(), []
    for a in out:                                   # the same ability under two keys once
        if a["does"].lower() not in seen:
            seen.add(a["does"].lower())
            unique.append(a)
    return unique[:MAX_ACTIONS]


_PLAN_PROMPT = """You turn one instruction for the running app "{app}" into steps.
You may use ONLY these actions (JSON):
{actions}

- Read what the user means, not their exact words: "go to my musubae chat" means open that session; pick names from "choices" when one fits.
- open_session only when the user asks to go to, continue or reopen a session. A message that merely resembles a session's title is just a message to send in the current session.
- A step is {{"do": "<an action's do>", "args": {{...}}}}, with only that action's args.
- Keep the user's own words for any message text.
- If any part of the instruction needs an action that is not listed, do not invent or stretch one: put that part, in the user's words, in "missing".

Answer with JSON only: {{"steps": [...], "missing": "<the part you could not map, or empty>"}}
Instruction: {text}"""


def _ask_model(prompt: str):
    from cygnus.rules import ai as rules_ai
    return rules_ai._ask_llm(prompt, local=False)


def _step_from(item: dict, by_do: dict) -> dict | None:
    """A model step checked against the app's actions; None when it isn't one."""
    action = by_do.get(str(item.get("do") or ""))
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    if not action:
        return None
    do = action["do"]
    if do == "new_session":
        return {"op": "new_session"}
    if do == "send":
        text = str(args.get("text") or "").strip()
        return {"op": "send", "text": text} if text else None
    if do == "open_session":
        name = str(args.get("name") or "").strip()
        return {"op": "open_session", "name": name} if name else None
    kind, key, ident = do.split(":", 2)
    allowed = {k: str(v) for k, v in args.items() if k in (action.get("args") or {})}
    return {"op": kind, "app": key, "id": ident, "args": allowed, "label": action["does"],
            "ask": bool(action.get("ask"))}


def understand(slug: str, text: str, rows=None, apps=None, sessions=None) -> dict:
    """What one "/slug text" asks: {"target", "steps", "missing", "actions", "by"}.
    `missing` is what the app has no action for; nothing runs then."""
    rows = running_apps.inventory(use_cache=False) if rows is None else rows
    apps = _apps() if apps is None else apps
    target = next((t for t in targets(rows, apps) if t["slug"] == slug), None)
    result = {"target": target, "steps": [], "missing": "", "actions": [], "by": "model"}
    if not target or not target["driver"]:
        return result
    row = next(r for r in rows if r["id"] == target["row"])
    if sessions is None and target["driver"] == "hermes-window":
        sessions = HermesWindow(row).session_titles()
    acts = actions(target, row, apps, sessions or ())
    result["actions"] = acts
    data, model = _ask_model(_PLAN_PROMPT.format(app=target["name"], text=text,
                                                 actions=json.dumps(acts, ensure_ascii=False)))
    if not data:
        result["by"] = "phrases"
        if target["driver"] == "hermes-window":
            result["steps"] = plan(text)
        else:
            result["missing"] = text
            result["no_model"] = True
        return result
    by_do = {a["do"]: a for a in acts}
    steps, unmapped = [], []
    for item in data.get("steps") or []:
        step = _step_from(item, by_do) if isinstance(item, dict) else None
        if step:
            steps.append(step)
        else:
            unmapped.append(str((item or {}).get("do") if isinstance(item, dict) else item))
    missing = str(data.get("missing") or "").strip()
    if unmapped and not missing:
        missing = text                              # the model reached for an action that isn't there
    result.update(steps=steps, missing=missing)
    return result


def cannot_reply(target: dict, missing: str, acts: list[dict], no_model: bool = False) -> str:
    """What to say when the app has no action for what was asked."""
    name = target["name"]
    if no_model:
        return (f"I need my model to read that for {name}, sir, and none is answering right now. "
                "Try again in a moment.")
    # Named actions only: a scanned button's name is the window's own text
    # (a Notepad tab read "Host 76.13.23.174. Modified.", 2026-09-15), so
    # those are counted, not read out.
    named = [a["does"] for a in acts if not a["do"].startswith("rule:") and not a.get("scanned")]
    scanned = sum(1 for a in acts if a.get("scanned"))
    rules = [a["does"] for a in acts if a["do"].startswith("rule:")]
    listing = "; ".join(named[:12]) or "nothing named yet"
    if scanned:
        listing += f"; and {scanned} button{'s' if scanned != 1 else ''} found in its window"
    reply = (f"I can't do \u201c{missing}\u201d in {name}, sir: it has no action for that. "
             f"What I can do there: {listing}.")
    if rules:
        reply += " Your rules for it: " + "; ".join(rules[:5]) + "."
    return reply + (f" To teach me, add a rule for {name} in the Apps panel (Rules), "
                    f"for example \u201c{missing}\u201d \u2192 the keys or clicks that do it.")


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


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def best_title(name: str, titles) -> str | None:
    """The session title a spoken name means: most shared words, at least half
    of the name's words ("my musubae chat" is "Musubae status check")."""
    want = _words(name) - {"my", "the", "a", "chat", "session", "conversation", "about", "on", "with"}
    if not want:
        return None
    scored = sorted(((len(want & _words(t)) / len(want), t) for t in titles), reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 0.5 else None


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
        """Send keys to Hermes only. If something else took the focus (on
        2026-09-15 Chrome did, between opening a session and pasting), bring
        Hermes back once; Chromium puts the focus back where it was in it."""
        from pywinauto import keyboard
        if not self._front():
            self._raise()
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
    def _buttons(win, name: str) -> list:
        return [c for c in win.descendants(control_type="Button") if (c.element_info.name or "") == name]

    @staticmethod
    def _texts(win, above: int) -> list[str]:
        out = []
        for c in win.descendants(control_type="Text"):
            info = c.element_info
            if info.name and info.rectangle.bottom <= above:
                out.append(info.name)
        return out

    def _session_rows(self, win) -> dict:
        """{title: the row button} from the sidebar. Each session shows as
        "Reorder <title>" (its drag handle) and "Reorder <title> <title> …"
        (the row that opens it)."""
        names = [(c, c.element_info.name or "") for c in win.descendants(control_type="Button")]
        names = [(c, n) for c, n in names if n.startswith("Reorder ")]
        rows = {}
        for _, n in names:
            title = n[len("Reorder "):]
            full = [(c, other) for c, other in names if other.startswith(f"Reorder {title} {title}")]
            if full:
                rows[title] = max(full, key=lambda p: len(p[1]))[0]
        return rows

    def session_titles(self) -> list[str]:
        """The sessions the sidebar shows, newest first; [] when it is hidden.
        Reading them changes nothing on screen."""
        return running_apps.ui_call(self._session_titles)

    def _session_titles(self) -> list[str]:
        try:
            win = self._window()
            win.descendants()                       # Chromium builds its tree once asked
            time.sleep(1.0)
            return list(self._session_rows(win))
        except Exception:
            return []

    def new_session(self) -> str:
        self._raise()
        self._keys("^n")
        time.sleep(1.5)
        return "Started a new Hermes session."

    def open_session(self, name: str) -> str:
        self._raise()
        win = self._window()
        rows = self._session_rows(win)
        if not rows:
            shown = [c for c in win.descendants(control_type="Button")
                     if (c.element_info.name or "").startswith("Show sidebar")]
            if shown:
                shown[0].invoke()
                time.sleep(1.0)
                rows = self._session_rows(win)
        title = best_title(name, rows)
        if not title:
            recent = ", ".join(f"\u201c{t}\u201d" for t in list(rows)[:5]) or "none I can see"
            raise DriveError(f"there's no Hermes session called \u201c{name}\u201d (recent ones: {recent})")
        rows[title].invoke()
        time.sleep(1.5)
        return f"Opened the Hermes session \u201c{title}\u201d."

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


def _run_app_step(step: dict, apps: dict) -> str:
    """An ability or a rule of an ordinary app, through the same runners the
    voice router uses."""
    from cygnus.brain import intent_router
    runner = intent_router._run_ability if step["op"] == "ability" else intent_router._run_rule
    reply, ok = runner(step["app"], step["id"], step.get("args") or {}, apps)
    if not ok:
        raise DriveError((reply or f"{step.get('label', step['id'])} didn't work").replace("[Error] ", "").rstrip("."))
    return reply


def _run_step(driver, step: dict, apps: dict) -> str:
    if step["op"] in ("ability", "rule"):
        return _run_app_step(step, apps)
    if step["op"] == "new_session":
        return driver.new_session()
    if step["op"] == "open_session":
        return driver.open_session(step["name"])
    reply = driver.send(step["text"])
    return f'{driver.label}, to "{_short(step["text"])}": {reply or "(no reply I could read)"}'


def run(slug: str, steps: list[dict], rows=None, apps=None) -> str:
    """Carry out the steps in the running app `slug`; what to tell the user.
    Drives windows, so it runs on the UI Automation thread."""
    if not _run_lock.acquire(blocking=False):
        return "Another app command is still running, sir. I'll take this one when it's done."
    try:
        return running_apps.ui_call(_run, slug, steps, rows, apps)
    finally:
        _run_lock.release()


def _run(slug: str, steps: list[dict], rows=None, apps=None) -> str:
    rows = running_apps.inventory(use_cache=False) if rows is None else rows
    apps = _apps() if apps is None else apps
    target = next((t for t in targets(rows, apps) if t["slug"] == slug), None)
    if not target:
        return f"Nothing called /{slug} is running, sir."
    if not target["driver"]:
        return f"I can't drive {target['name']} yet, sir: it has no actions or rules I can use."
    row = next(r for r in rows if r["id"] == target["row"])
    driver = DRIVERS[target["driver"]](row) if target["driver"] in DRIVERS else None
    done = []
    for step in steps:
        try:
            done.append(_run_step(driver, step, apps))
        except DriveError as e:
            done.append(f"I stopped: {e}.")
            break
    return "\n".join(done)
