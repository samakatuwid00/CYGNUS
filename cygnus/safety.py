"""safety.py - one gate in front of every action Cygnus takes on the machine.

Before this, each path had its own checks or none. The agent loop has no
confirm gate, so "delete the file C:\\...\\old.txt" opened PowerShell unasked
(smoke run, 2026-09-22), and a map of the code found more paths that reached
the machine with no check at all: text sent to Hermes, a full agent, through
app_step; typing and Enter into a terminal through an app's abilities;
close_application with force; open_application on a script path;
write_to_notepad writing anywhere and overwriting silently; and tools whose
own confirm flag the model could set in the same turn it was asked.

The gate sits where actions run, not where a request is routed, so it holds
whichever path picked the action:

  tools.execute_tool       every model tool and local fast path
  agent_loop._run_tool     app_step (Hermes, abilities, rules in a running app)
  app_abilities.run_ability  typing, keys and clicks in a terminal

assess() says what an action needs: "act" (go), "confirm" (ask first) or
"block" (never). A confirm is held here, bound to that exact action, and runs
only when a later turn says yes (confirm_pending); a no drops it. The model
cannot answer its own question: a tool's confirm flag counts only when that
tool asked in an earlier turn.
"""

from __future__ import annotations

import os
import re
import threading
import time

CONFIRM_TTL_S = 300.0

# Programs that run commands: typing into one, or pressing Enter in it, is
# running a command.
TERMINAL_APPS = {"powershell", "pwsh", "cmd", "terminal", "windowsterminal", "wt", "conhost",
                 "bash", "git-bash", "gitbash", "wsl", "ubuntu", "putty", "mintty", "alacritty",
                 "warp", "tabby", "hyper"}
# Ability ops that act inside the app's window.
_INPUT_OPS = {"type", "key", "click"}
# A path that would run code when opened.
_RUNNABLE_RE = re.compile(r"\.(exe|bat|cmd|com|ps1|psm1|vbs|vbe|js|jse|wsf|wsh|msi|msc|scr|hta|reg|lnk|py|pyw)\s*$",
                          re.I)
# Tools whose own confirm flag the model sets on the second call; it counts
# only when the first call (which asked) was in an earlier turn.
SELF_LATCHED = {"desktop_control", "run_opencli", "delegate_to_hermes", "run_autonomous", "delegate"}

_lock = threading.Lock()
_turn = 0
_asked_turn: dict[str, int] = {}        # tool -> turn it last asked for a confirm
_pending: dict | None = None            # {"key", "question", "run", "ts", "turn"}


class Decision:
    __slots__ = ("kind", "reason", "question")

    def __init__(self, kind: str, reason: str = "", question: str = ""):
        self.kind, self.reason, self.question = kind, reason, question

    def __repr__(self):
        return f"Decision({self.kind!r}, {self.reason!r})"


ACT = Decision("act")


# ------------------------------------------------------------------ turns --

def begin_turn() -> int:
    """The brain calls this once per user turn, before anything runs."""
    global _turn
    with _lock:
        _turn += 1
        return _turn


def current_turn() -> int:
    return _turn


def note_asked(tool: str, result) -> None:
    """A tool's own confirm question went out this turn (its result says
    NEEDS_CONFIRM); remember when, so its confirm flag counts only later."""
    text = str(result or "")
    if "NEEDS_CONFIRM" in text[:40]:
        with _lock:
            _asked_turn[tool] = _turn


def confirm_flag_allowed(tool: str) -> bool:
    """The model set confirm=true on `tool`: allowed only if the tool asked
    in an earlier turn (the user has heard the question and answered)."""
    with _lock:
        asked = _asked_turn.get(tool)
    return asked is not None and asked < _turn


# --------------------------------------------------------------- policy --

def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on") if not isinstance(v, bool) else v


def _is_terminal(app: str) -> bool:
    slug = re.sub(r"[^a-z0-9-]", "", str(app or "").lower().replace(" ", ""))
    return slug in TERMINAL_APPS or slug.startswith(("windowsterminal", "powershell"))


def assess(tool: str, args: dict | None = None) -> Decision:
    """What running `tool` with `args` needs: act, confirm or block."""
    args = args or {}
    if tool == "close_application" and _truthy(args.get("force", False)):
        app = args.get("app", "")
        return Decision("confirm", "force-closing kills the program without saving",
                        f"Force-close {app}? Anything unsaved in it is lost, sir.")
    if tool == "open_application":
        app = str(args.get("app") or "")
        if ("\\" in app or "/" in app) and _RUNNABLE_RE.search(app):
            return Decision("confirm", "opening a program or script file runs it",
                            f"Run {os.path.basename(app)}? Opening that file runs it, sir.")
    if tool == "write_to_notepad" and str(args.get("filename") or "").strip():
        try:
            from cygnus import tools
            p = tools._resolve_write_path(str(args["filename"]).strip())
            if p.suffix == "":
                p = p.with_suffix(".txt")
            if not tools._is_allowed_path(p):
                return Decision("block", f"{p} is outside the folders Cygnus may write to")
            if p.exists():
                return Decision("confirm", "the file exists and would be replaced",
                                f"{p.name} already exists. Replace it, sir?")
        except Exception:
            pass
    if tool == "write_file" and _truthy(args.get("overwrite", False)):
        return Decision("confirm", "overwriting replaces the file",
                        f"Replace {os.path.basename(str(args.get('path') or 'that file'))}, sir?")
    return ACT


def assess_app_step(slug: str, step: dict) -> Decision:
    """An action inside a running app (agent_loop app_step)."""
    step = step or {}
    do = str(step.get("do") or step.get("id") or "")
    args = step.get("args") or {}
    if slug == "hermes" and do == "send":
        text = str(args.get("text") or "")
        try:
            from cygnus import tools
            safe = tools._is_safe_task(text)
        except Exception:
            safe = False
        if not safe:
            return Decision("confirm", "Hermes is an agent and the request may change things",
                            f"Send this to Hermes: \u201c{text[:160]}\u201d? It may change files, sir.")
    if _is_terminal(slug) or _is_terminal(str(step.get("app") or "")):
        if do not in ("new_session", "open_session") and not do.endswith((".open", ":open", "focus")):
            return Decision("confirm", "typing or pressing keys in a terminal runs commands",
                            f"Do \u201c{step.get('label') or do}\u201d in the terminal, sir? That can run commands.")
    return ACT


def assess_ability(app: str, ability: dict) -> Decision:
    """One app ability (app_abilities.run_ability), by what its steps do."""
    ops = {str(op.get("op")) for op in (ability or {}).get("how") or []}
    if _is_terminal(app) and ops & _INPUT_OPS:
        return Decision("confirm", "typing or pressing keys in a terminal runs commands",
                        f"Run \u201c{ability.get('name') or ability.get('id')}\u201d in the terminal, sir? "
                        "That can run commands.")
    return ACT


# ---------------------------------------------------------------- latch --

def current_client() -> str:
    """Which HUD this turn came from ("default" when nothing says).

    Imported lazily: brain.core imports this module, not the other way round.
    """
    try:
        from cygnus.brain.core import _client_key
        return _client_key() or "default"
    except Exception:
        return "default"


def hold(key: str, decision: Decision, run) -> str:
    """Keep `run` until the user answers; the reply to speak now."""
    global _pending
    with _lock:
        _pending = {"key": key, "question": decision.question, "reason": decision.reason,
                    "run": run, "ts": time.time(), "turn": _turn,
                    # The question was asked on one device. Only that device can
                    # answer it: on 2026-09-23 a "yes" typed on the desktop
                    # released a delete held for the phone (bench/concurrency).
                    "client": current_client()}
    _audit("held", key, decision.reason)
    return f"[NEEDS_CONFIRM:safety] {decision.question}"


def pending() -> dict | None:
    """The held action, if one is waiting and fresh."""
    global _pending
    with _lock:
        if _pending and time.time() - _pending["ts"] > CONFIRM_TTL_S:
            _pending = None
        return dict(_pending) if _pending else None


def pending_ts() -> float:
    p = pending()
    return p["ts"] if p else 0.0


def held_elsewhere() -> str | None:
    """The question waiting on ANOTHER device, or None.

    So a "yes" that answers nothing here can say why instead of being routed
    as a fresh request.
    """
    p = pending()
    return p["question"] if p and p.get("client", "default") != current_client() else None


def confirm_pending():
    """The user said yes in a later turn: run the held action. None when
    nothing is held (or it was held this same turn: not an answer, or it was
    asked on another device)."""
    global _pending
    with _lock:
        p = _pending
        if not p or time.time() - p["ts"] > CONFIRM_TTL_S or p["turn"] >= _turn:
            return None
        if p.get("client", "default") != current_client():
            # Not this device's question to answer. Leave it held for the one
            # that was asked.
            _audit("confirm_from_another_client", p["key"], p["reason"])
            return None
        _pending = None
    _audit("confirmed", p["key"], p["reason"])
    try:
        return p["run"]()
    except Exception as e:
        return f"[Error] {type(e).__name__}: {e}"


def decline_pending() -> str | None:
    global _pending
    with _lock:
        p = _pending
        if p and p.get("client", "default") != current_client():
            return None          # someone else's question; not this device's "no"
        _pending = None
    if not p:
        return None
    _audit("declined", p["key"], p["reason"])
    return "Okay, I won't do that, sir."


def gate(key: str, decision: Decision, run):
    """Run now, hold for a confirm, or refuse. Returns what `run` returns, the
    question to speak, or the refusal."""
    if decision.kind == "block":
        _audit("blocked", key, decision.reason)
        return f"[Blocked] {decision.reason[:1].upper()}{decision.reason[1:]}, sir."
    if decision.kind == "confirm":
        return hold(key, decision, run)
    return run()


def _audit(decision: str, key: str, reason: str) -> None:
    try:
        from cygnus import tools
        tools._audit_log("safety", key[:300], decision, extra={"reason": reason})
    except Exception:
        pass


def reset() -> None:
    """Forget the held action and the turn history (tests)."""
    global _pending, _turn
    with _lock:
        _pending = None
        _asked_turn.clear()
        _turn = 0
