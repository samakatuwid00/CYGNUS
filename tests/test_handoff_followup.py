"""A delegation is routed as one, what it made is opened only when asked,
and its answer stays in the conversation (2026-09-14: the implementation
request read a window's text instead; "open it in Chrome" was refused to
Claude; "I approved it! Proceed" found the conversation wiped)."""

import time

import pytest

from cygnus.brain import core as brain_gemini
from cygnus import tools

SMOKE = ("Delegate to Claude Code: implement the design in running_panel_mockup.html into the real "
         "Running panel in jarvis_hud_v3.html. Merge Active Tasks and Running Now into one panel like "
         "the mockup. Keep the phone features: Show, Read text, Describe and the window picture. Keep "
         "the stop button laptop-only. Don't commit.")


@pytest.mark.parametrize("text", [
    SMOKE,
    "use claude code to read the terminal window and fix the error",
    "ask hermes to describe the chrome window layout in a report"])
def test_a_handoff_is_not_a_window_command(text):
    assert not brain_gemini.classify_intent(text).startswith("running_")


@pytest.mark.parametrize("text, intent", [
    ("read the terminal", "running_text"), ("describe the chrome window", "running_describe"),
    ("bring chrome to the front", "running_front"), ("stop the leftover tail", "running_stop")])
def test_short_window_commands_still_route(text, intent):
    assert brain_gemini.classify_intent(text) == intent


@pytest.mark.parametrize("text, own", [
    ("what did Claude Code just change in the Running panel?", True),
    ("what did hermes do?", True), ("did claude finish the panel?", True),
    ("what changed?", True), ("which files did it change?", True),
    ("what did the task add to the HUD?", True),
    ("change the running panel colours", False), ("what is claude doing", False),
    ("delegate to claude code: fix what changed in the panel", False)])
def test_a_question_about_a_handed_off_job_is_answered_from_the_record(text, own):
    assert brain_gemini._asks_about_own_work(text) is own


@pytest.fixture
def launched(monkeypatch):
    from cygnus.browser import agent as browser_agent
    calls = []
    monkeypatch.setattr(browser_agent, "_chrome_exe", lambda: __file__)      # any existing file
    monkeypatch.setattr(tools.subprocess, "Popen", lambda args, **kw: calls.append(args))
    return calls


FIRST = ("delegate to claude code to improve you running task panel into more organized and uniform "
         "design format, create a ui mockup and open it in the chrome so I can see its output")


def test_the_file_is_opened_when_the_request_asked_to_see_it(launched, tmp_path):
    page = tmp_path / "running_panel_mockup.html"
    page.write_text("<p>mockup</p>")
    result = f"I made the mockup.\n\nFiles:\n- `{page}`\n- `{tmp_path / 'shot.png'}`"
    assert tools.open_after_job(FIRST, result) == "I've opened running_panel_mockup.html in Chrome for you."
    assert launched == [[__file__, str(page)]]


def test_a_name_relative_to_cygnus_folder_is_found(launched, tmp_path, monkeypatch):
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "a.png").write_bytes(b"x")
    (tmp_path / "running_panel_mockup.html").write_text("x")
    monkeypatch.setattr(tools, "_REPO_DIR", str(tmp_path))
    result = "Screenshots in shots\\a.png; the page is running_panel_mockup.html in jarvis-demo."
    assert tools._result_file(result) == str(tmp_path / "running_panel_mockup.html")   # pages first


def test_nothing_is_opened_unless_asked(launched, tmp_path):
    page = tmp_path / "page.html"
    page.write_text("x")
    assert tools.open_after_job(SMOKE, f"Built it; see `{page}`.") == ""
    assert launched == []


def test_asked_but_no_file_says_so(launched):
    assert tools.open_after_job(FIRST, "Done, nothing was written.") == \
        "You asked to see it, but the answer names no file I could open."
    assert launched == []


def test_a_finished_job_joins_the_askers_history_and_counts_as_activity(monkeypatch):
    b = brain_gemini.JarvisBrain()
    # through monkeypatch, so later tests get this thread's client back
    monkeypatch.setattr(brain_gemini._progress_local, "client", "remote", raising=False)
    b.conversation = [{"role": "user", "content": "delegate the mockup"}]
    b._last_turn_ts = time.time() - 3600
    b.note_late_result("If you like the design, I can build it into the real panel next.", client="remote")
    assert b.conversation[-1]["content"].endswith("I can build it into the real panel next.")
    assert time.time() - b._last_turn_ts < 5
    brain_gemini._progress_local.client = "local"
    assert b.conversation == []                     # another client's history is its own


def test_the_idle_reset_waits_while_a_job_runs(monkeypatch):
    from cygnus.jobs import registry as jobs
    b = brain_gemini.JarvisBrain()
    monkeypatch.setattr(brain_gemini._progress_local, "client", "remote", raising=False)
    b.conversation = [{"role": "user", "content": "delegate the mockup"}]
    b._last_turn_ts = time.time() - 3600
    monkeypatch.setattr(jobs, "active", lambda: [{"id": "32fdcb73"}])
    b._maybe_reset_idle()
    assert b.conversation
    monkeypatch.setattr(jobs, "active", lambda: [])
    b._maybe_reset_idle()
    assert b.conversation == []
