"""Prompting a Claude Code session that is already open (claude_prompt.py)."""

import json
import threading
import time
import types

import pytest

from cygnus.apps import claude_prompt as cp


def _row(folder, title, status="idle", transcript="t.jsonl"):
    return {"name": f"Claude Code in {folder}", "group": "agents", "status": status,
            "session": {"folder": folder, "title": title, "transcript": transcript}}


ROWS = [_row("CYGNUS", "CYGNUS listening feature accuracy"), _row("my-app", "Portfolio design overhaul"),
        {"name": "Google Chrome", "group": "apps"}]


def test_norm_drops_claudes_spinner_before_the_title():
    assert cp._norm("◐ Portfolio design overhaul") == "portfolio design overhaul"
    assert cp._norm("✳ Fix it") == "fix it"


@pytest.mark.parametrize("said, folder", [
    ("my-app", "my-app"), ("my app", "my-app"), ("in my-app", "my-app"), ("the portfolio", "my-app"),
    ("Cygnus", "CYGNUS"), ("claude code in cygnus", "CYGNUS"), ("listening", "CYGNUS"), ("myapp", "my-app"),
])
def test_pick_finds_the_session_the_owner_named(said, folder):
    assert cp.pick(said, ROWS)["folder"] == folder


def test_pick_with_no_name_takes_the_only_session_or_asks_which():
    assert cp.pick(None, ROWS[1:])["folder"] == "my-app"
    with pytest.raises(cp.PromptError, match="which Claude Code session"):
        cp.pick(None, ROWS)
    with pytest.raises(cp.PromptError, match="no Claude Code session is open"):
        cp.pick("my-app", ROWS[2:])
    with pytest.raises(cp.PromptError, match="no Claude Code session called"):
        cp.pick("kubernetes", ROWS)


def _rec(kind, ts, blocks):
    return json.dumps({"type": kind, "timestamp": ts, "message": {"content": blocks}})


def test_reply_waits_for_the_last_message_with_no_tool_pending(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    monkeypatch.setattr(cp.running_apps, "_stamp", lambda v: float(v) if v is not None else None)
    lines = [_rec("assistant", 90, [{"type": "text", "text": "old answer"}]),
             _rec("user", 100, [{"type": "text", "text": "run the tests"}])]
    path.write_text("\n".join(lines))
    assert cp.reply_since(str(path), 100) is None                   # nothing yet
    lines += [_rec("assistant", 101, [{"type": "text", "text": "Running."}, {"type": "tool_use", "name": "Bash"}])]
    path.write_text("\n".join(lines))
    assert cp.reply_since(str(path), 100) is None                   # a tool is running
    lines += [_rec("user", 102, [{"type": "tool_result", "content": "12 passed"}]),
              _rec("assistant", 103, [{"type": "text", "text": "All 12 tests pass."}])]
    path.write_text("\n".join(lines))
    assert cp.reply_since(str(path), 100) == "All 12 tests pass."


def test_summary_speaks_the_first_sentences_without_markdown():
    text = ("**Done:** the wake word now works over music. I changed `wake.py` and added tests.\n\n"
            "| a | b |\n|---|---|\n```python\nx = 1\n```\n- More detail here that goes on for a long while " * 3)
    s = cp.summary(text)
    assert s.startswith("Done: the wake word now works over music.")
    assert "`" not in s and "|" not in s and "```" not in s and len(s) <= cp.SUMMARY_CHARS


def _session(tmp_path):
    return {"name": "Claude Code in my-app", "folder": "my-app", "title": "Portfolio design overhaul",
            "transcript": str(tmp_path / "t.jsonl")}


def test_the_prompt_is_sent_after_the_read_back_and_the_answer_spoken(tmp_path, monkeypatch):
    s = _session(tmp_path)
    monkeypatch.setattr(cp, "POLL_S", 0.05)
    sent, told, done = [], [], threading.Event()

    def send(session, text):
        sent.append(text)
        now = time.time()
        (tmp_path / "t.jsonl").write_text("\n".join([
            json.dumps({"type": "user", "timestamp": now, "message": {"content": text}}),
            _rec("assistant", now + 1, [{"type": "text", "text": "Tests pass. Nothing else changed."}])]))
    monkeypatch.setattr(cp.running_apps, "_stamp", lambda v: float(v) if v is not None else None)
    say = cp.schedule(s, "run  the tests", on_done=lambda t: (told.append(t), done.set()), delay=0.1, send=send)
    assert "Sending to Claude Code in my-app" in say and "cancel" in say
    assert done.wait(5)
    assert sent == ["run the tests"]
    assert told == ["Claude in my-app: Tests pass. Nothing else changed."]


def test_cancel_before_the_send_sends_nothing(tmp_path):
    s = _session(tmp_path)
    sent = []
    cp.schedule(s, "delete the build folder", delay=0.5, send=lambda se, t: sent.append(t))
    assert cp.pending()["text"] == "delete the build folder"
    assert cp.decline_pending() == "Cancelled. Nothing was sent to Claude Code in my-app, sir."
    time.sleep(0.8)
    assert sent == [] and cp.pending() is None
    assert cp.decline_pending() is None                              # nothing waits now


def test_a_failed_send_is_reported(tmp_path):
    told, done = [], threading.Event()

    def send(session, text):
        raise cp.PromptError("I can't find the terminal window of Claude Code in my-app")
    cp.schedule(_session(tmp_path), "run the tests", on_done=lambda t: (told.append(t), done.set()),
                delay=0.05, send=send)
    assert done.wait(3)
    assert told[0].startswith("I didn't send it to Claude Code in my-app: I can't find the terminal window")


def test_enter_is_never_pressed_when_the_session_is_not_in_front(tmp_path, monkeypatch):
    """Focus stolen after the paste: no Enter, so the text can't run anywhere."""
    import sys
    keys = []
    fake_kb = types.SimpleNamespace(send_keys=keys.append)
    monkeypatch.setitem(sys.modules, "pywinauto", types.SimpleNamespace(keyboard=fake_kb))
    monkeypatch.setitem(sys.modules, "pywinauto.keyboard", fake_kb)
    from cygnus.apps import commands
    monkeypatch.setattr(commands, "_clip_get", lambda: "")
    monkeypatch.setattr(commands, "_clip_set", lambda t: None)
    monkeypatch.setattr(cp, "_bring", lambda s: 42)
    fronts = iter([True, False])
    monkeypatch.setattr(cp, "_front", lambda h, t: next(fronts))
    with pytest.raises(cp.PromptError, match="didn't press Enter"):
        cp._type(_session(tmp_path), "run the tests")
    assert keys == ["^v"]


def test_agent_tool_sends_the_owners_words_when_the_model_dropped_a_constraint(monkeypatch):
    from cygnus.brain import agent_loop as al
    got = {}
    monkeypatch.setattr(cp, "pick", lambda name, rows=None: {"name": "Claude Code in CYGNUS", "folder": "CYGNUS"})
    monkeypatch.setattr(cp, "schedule", lambda s, t, on_done=None: got.setdefault("text", t) and "ok")
    owner = "tell Claude in cygnus to fix the wake word but don't touch the tests"
    al._run_tool("prompt_claude", {"session": "cygnus", "prompt": "fix the wake word"}, {"user_text": owner})
    assert got["text"] == "fix the wake word but don't touch the tests"


def test_agent_tool_reports_why_it_could_not_send(monkeypatch):
    from cygnus.brain import agent_loop as al

    def none(name, rows=None):
        raise cp.PromptError("no Claude Code session is open")
    monkeypatch.setattr(cp, "pick", none)
    assert al._run_tool("prompt_claude", {"prompt": "hi"}, {"user_text": "tell claude hi"}) == \
        "[Error] no Claude Code session is open"
