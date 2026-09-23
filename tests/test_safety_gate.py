"""safety.py: one gate where actions run. What needs a confirm, that a held
action runs only on a yes in a later turn, that the model cannot confirm its
own question, and that each chokepoint (execute_tool, agent_loop app_step,
app_abilities.run_ability) goes through it."""

import json
from types import SimpleNamespace as NS

import pytest

from cygnus import safety
from cygnus import tools


@pytest.fixture(autouse=True)
def fresh():
    safety.reset()
    yield
    safety.reset()


# ------------------------------------------------------------- policy --

@pytest.mark.parametrize("tool, args, kind", [
    ("close_application", {"app": "notepad"}, "act"),
    ("close_application", {"app": "notepad", "force": True}, "confirm"),
    ("close_application", {"app": "notepad", "force": "true"}, "confirm"),
    ("open_application", {"app": "spotify"}, "act"),
    ("open_application", {"app": r"C:\Users\me\Downloads\setup.bat"}, "confirm"),
    ("open_application", {"app": r"C:\tools\evil.ps1"}, "confirm"),
    ("write_file", {"path": "notes.txt", "content": "x"}, "act"),
    ("write_file", {"path": "notes.txt", "content": "x", "overwrite": True}, "confirm"),
    ("read_file", {"path": r"C:\Windows\win.ini"}, "act"),
])
def test_what_each_action_needs(tool, args, kind):
    assert safety.assess(tool, args).kind == kind


def test_write_to_notepad_outside_the_allowed_folders_is_refused():
    d = safety.assess("write_to_notepad", {"content": "x", "filename": r"C:\Windows\System32\drivers\etc\hosts"})
    assert d.kind == "block"


def test_write_to_notepad_over_an_existing_file_asks(tmp_path, monkeypatch):
    f = tmp_path / "note.txt"
    f.write_text("old")
    monkeypatch.setattr(tools, "_is_allowed_path", lambda p: True)
    assert safety.assess("write_to_notepad", {"content": "x", "filename": str(f)}).kind == "confirm"
    assert safety.assess("write_to_notepad", {"content": "x", "filename": str(tmp_path / "new.txt")}).kind == "act"


@pytest.mark.parametrize("slug, step, kind", [
    ("hermes", {"do": "send", "args": {"text": "what is the weather today?"}}, "act"),
    ("hermes", {"do": "send", "args": {"text": "delete my old downloads"}}, "confirm"),
    ("hermes", {"do": "new_session"}, "act"),
    ("terminal", {"do": "ability:terminal:terminal.type", "label": "Type a command"}, "confirm"),
    ("terminal", {"do": "ability:powershell:powershell.open", "label": "Open PowerShell"}, "act"),
    ("notepad", {"do": "ability:notepad:notepad.type"}, "act"),
])
def test_what_an_app_step_needs(slug, step, kind):
    assert safety.assess_app_step(slug, step).kind == kind


def test_terminal_input_abilities_ask_and_others_do_not():
    typing = {"id": "t", "name": "Type a command", "how": [{"op": "type", "target": "input"}]}
    assert safety.assess_ability("powershell", typing).kind == "confirm"
    assert safety.assess_ability("windowsterminal", {"how": [{"op": "key", "keys": "{ENTER}"}]}).kind == "confirm"
    assert safety.assess_ability("powershell", {"how": [{"op": "launch"}]}).kind == "act"
    assert safety.assess_ability("notepad", typing).kind == "act"


# -------------------------------------------------------------- latch --

def test_a_held_action_runs_only_on_a_yes_in_a_later_turn():
    ran = []
    safety.begin_turn()
    out = safety.gate("x", safety.Decision("confirm", "why", "Do it, sir?"), lambda: ran.append(1) or "done")
    assert out == "[NEEDS_CONFIRM:safety] Do it, sir?" and ran == []
    assert safety.confirm_pending() is None                  # same turn: not an answer
    safety.begin_turn()
    assert safety.confirm_pending() == "done" and ran == [1]
    assert safety.confirm_pending() is None                  # used up


def test_no_drops_it():
    safety.begin_turn()
    safety.gate("x", safety.Decision("confirm", "why", "Do it?"), lambda: pytest.fail("ran"))
    safety.begin_turn()
    assert safety.decline_pending() == "Okay, I won't do that, sir."
    assert safety.confirm_pending() is None


def test_a_stale_hold_expires(monkeypatch):
    safety.begin_turn()
    safety.gate("x", safety.Decision("confirm", "why", "Do it?"), lambda: "done")
    safety.begin_turn()
    monkeypatch.setattr(safety, "CONFIRM_TTL_S", -1)
    assert safety.confirm_pending() is None


def test_a_block_never_runs():
    out = safety.gate("x", safety.Decision("block", "that is outside the allowed folders"), lambda: pytest.fail("ran"))
    assert out == "[Blocked] That is outside the allowed folders, sir."


# ----------------------------------------------------- execute_tool --

def test_execute_tool_holds_a_force_close_until_yes(monkeypatch):
    closed = []
    monkeypatch.setitem(tools.TOOL_MAP, "close_application",
                        lambda **kw: closed.append(kw) or f"Closed {kw['app']}.")
    safety.begin_turn()
    out = tools.execute_tool("close_application", {"app": "notepad", "force": True})
    assert out.startswith("[NEEDS_CONFIRM:safety] Force-close notepad") and closed == []
    safety.begin_turn()
    assert safety.confirm_pending() == "Closed notepad."
    assert closed == [{"app": "notepad", "force": True}]     # the marker never reaches the tool


def test_execute_tool_runs_a_plain_close_at_once(monkeypatch):
    monkeypatch.setitem(tools.TOOL_MAP, "close_application", lambda **kw: "Closed.")
    assert tools.execute_tool("close_application", {"app": "notepad"}) == "Closed."


def test_the_model_cannot_confirm_in_the_turn_that_asked(monkeypatch):
    seen = []

    def desktop(**kw):
        seen.append(kw.get("confirm"))
        return "Done." if kw.get("confirm") else "NEEDS_CONFIRM: I will: click OK. Say 'confirm' to proceed."
    monkeypatch.setitem(tools.TOOL_MAP, "desktop_control", desktop)
    safety.begin_turn()
    tools.execute_tool("desktop_control", {"task": "click OK"})
    out = tools.execute_tool("desktop_control", {"task": "click OK", "confirm": True})   # same turn
    assert seen == [None, False] and out.startswith("NEEDS_CONFIRM")
    safety.begin_turn()                                       # the user answered
    assert tools.execute_tool("desktop_control", {"task": "click OK", "confirm": True}) == "Done."
    assert seen[-1] is True


# ------------------------------------------------ agent_loop app_step --

def test_app_step_holds_a_risky_hermes_send(monkeypatch):
    from cygnus.brain import agent_loop as al
    from cygnus.apps import commands as app_commands
    from cygnus.apps import running as running_apps
    target = {"slug": "hermes", "name": "Hermes", "row": 1, "driver": "hermes-window"}
    monkeypatch.setattr(al, "_target", lambda app: (target, [target]))
    monkeypatch.setattr(running_apps, "inventory", lambda use_cache=True: [{"id": 1}])
    monkeypatch.setattr(app_commands, "HermesWindow", lambda row: NS(session_titles=lambda: ()))
    monkeypatch.setattr(app_commands, "actions", lambda *a: [{"do": "send"}])
    monkeypatch.setattr(app_commands, "_apps", lambda: {})
    monkeypatch.setattr(app_commands, "_step_from", lambda d, acts: {"do": "send", "app": "hermes", "id": "send",
                                                                     "label": "Send", "args": d["args"]})
    sent = []
    monkeypatch.setattr(app_commands, "run", lambda slug, steps, rows=None: sent.append(steps) or "Sent.")
    safety.begin_turn()
    out = al._run_tool("app_step", {"app": "hermes", "do": "send",
                                    "args": json.dumps({"text": "delete my old downloads"})}, {"client": "t"})
    assert out.startswith("[NEEDS_CONFIRM:safety] Send this to Hermes") and sent == []
    safety.begin_turn()
    assert safety.confirm_pending() == "Sent." and len(sent) == 1
    safe = al._run_tool("app_step", {"app": "hermes", "do": "send",
                                     "args": json.dumps({"text": "what is the weather today?"})}, {"client": "t"})
    assert safe == "Sent."


# -------------------------------------------------- run_ability --

def test_run_ability_holds_terminal_typing_but_not_the_panel_test(monkeypatch):
    from cygnus.apps import abilities as app_abilities
    ability = {"id": "ps.type", "name": "Type a command", "how": [{"op": "type", "target": "input", "text": "{cmd}"}],
               "needs": {"cmd": "the command"}}
    monkeypatch.setattr(app_abilities, "load_apps", lambda: {"apps": {"powershell": {"abilities": [ability]}}})
    ops = []
    monkeypatch.setattr(app_abilities, "_run_op", lambda key, entry, op, args: ops.append(op["op"]) or "")
    safety.begin_turn()
    out = app_abilities.run_ability("powershell", "ps.type", {"cmd": "Remove-Item x"})
    assert out.startswith("[NEEDS_CONFIRM:safety] Run") and ops == []
    assert app_abilities.run_ability("powershell", "ps.type", {"cmd": "dir"}, confirmed=True) == "Done: Type a command."
    assert ops == ["type"]


# ------------------------------------------------ the brain: yes / no --

def test_a_bare_yes_releases_the_held_action_and_no_drops_it(monkeypatch):
    from cygnus.brain import agent_loop
    from cygnus.brain import core as brain_gemini
    monkeypatch.setattr(agent_loop, "MODE", "off")
    b = brain_gemini.JarvisBrain()
    safety.begin_turn()
    safety.gate("x", safety.Decision("confirm", "why", "Force-close notepad?"), lambda: "Closed notepad.")
    assert b.think("yes") == "Closed notepad."
    safety.begin_turn()
    safety.gate("y", safety.Decision("confirm", "why", "Force-close notepad?"), lambda: pytest.fail("ran"))
    assert b.think("no") == "Okay, I won't do that, sir."


# ------------------------------- defaults for someone who is not the owner --

def test_terminal_abilities_are_off_until_switched_on():
    """Typing into a terminal is running commands. A new install has those
    abilities present but off, so they cannot fire before the owner has
    looked at them in the Apps panel."""
    from cygnus.apps import abilities as app_abilities
    typed = app_abilities._ab("powershell", "type", "Type a command", "type ...",
                              [{"op": "type", "target": "input", "text": "{cmd}"}], verify=None)
    assert typed["enabled"] is False and typed["level"] == "ask"
    launch = app_abilities._ab("powershell", "open", "Open PowerShell", "open powershell",
                               [{"op": "launch"}], verify=None)
    assert launch["enabled"] is True                      # opening it is not typing in it
    other = app_abilities._ab("notepad", "type", "Type", "type ...",
                              [{"op": "type", "target": "Edit", "text": "{text}"}], verify=None)
    assert other["enabled"] is True                       # only terminals start off
