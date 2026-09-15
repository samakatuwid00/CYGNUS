"""app_commands: "/app instruction" split into steps, the "/" menu built from
what is running, and a run that goes step by step and stops at a failure."""

import threading

import pytest

import app_commands as ac


def test_the_owners_example_becomes_three_steps():
    text = ("start a new session then prompt, what is the status of second brain "
            "then ask hermes to delegate it to claude")
    assert ac.plan(text) == [{"op": "new_session"},
                             {"op": "send", "text": "what is the status of second brain"},
                             {"op": "send", "text": "delegate it to claude"}]


@pytest.mark.parametrize("text, steps", [
    ("what is the status of second brain", [{"op": "send", "text": "what is the status of second brain"}]),
    ("new session", [{"op": "new_session"}]),
    ("prompt: explain the wake word; then ask it to add tests",
     [{"op": "send", "text": "explain the wake word"}, {"op": "send", "text": "add tests"}]),
    ('prompt: "summarise A then B"', [{"op": "send", "text": "summarise A then B"}]),
    ("tell me a joke", [{"op": "send", "text": "tell me a joke"}])])
def test_instructions_split_into_steps(text, steps):
    assert ac.plan(text) == steps


@pytest.mark.parametrize("text, message", [
    ("on the new session prompt what is the status of musubae? show it on the screen too",
     "what is the status of musubae? show it on the screen too"),
    ("start a new session and prompt what is the status of second brain", "what is the status of second brain"),
    ("new session: hi", "hi"),
    ("in a new chat, ask hermes to list my open tasks", "list my open tasks"),
    ("what is the status of second brain in a new session", "what is the status of second brain")])
def test_a_new_session_is_heard_wherever_it_is_said(text, message):
    assert ac.plan(text) == [{"op": "new_session"}, {"op": "send", "text": message}]


def test_a_session_word_inside_the_message_is_left_alone():
    assert ac.plan("summarise what we did in the last session") == \
        [{"op": "send", "text": "summarise what we did in the last session"}]


def test_a_hermes_reply_is_tidied_for_speaking():
    raw = ("Musubae: pending top priority.\n\n"
           '::preview{file="C:/Users/deped/AppData/Local/Temp/musubae-status.html"}')
    assert ac.tidy_reply(raw) == "Musubae: pending top priority.\n\n(Hermes made musubae-status.html; it's open in Hermes.)"


def test_steps_read_as_the_preview_shows_them():
    assert [ac.describe(s) for s in ac.plan("new session then prompt: hi")] == ["New session", 'Send: "hi"']


def row(rid, name, group="apps", windows=True, doing="open"):
    return {"id": rid, "name": name, "group": group, "doing": doing,
            "windows": [{"hwnd": int(rid), "title": name}] if windows else []}


ROWS = [row("10", "Google Chrome"), row("11", "Notepad"), row("12", "Notepad"),
        row("13", "Hermes desktop app", group="agents"), row("14", "Hermes gateway", group="agents", windows=False),
        row("15", "Cygnus"), row("16", "node", group="servers")]


def test_the_menu_lists_running_windows_drivable_first():
    got = [(t["slug"], t["driver"]) for t in ac.targets(ROWS)]
    assert got == [("hermes", "hermes-window"), ("chrome", None), ("notepad", None), ("notepad-2", None)]


class FakeDriver:
    label = "Hermes"
    calls = []
    fail_on = None

    def __init__(self, row):
        self.row = row

    def new_session(self):
        FakeDriver.calls.append("new")
        return "Started a new Hermes session."

    def send(self, text):
        if text == FakeDriver.fail_on:
            raise ac.DriveError("the focus left Hermes, so I stopped before typing")
        FakeDriver.calls.append(text)
        return "READY"


@pytest.fixture
def fake(monkeypatch):
    FakeDriver.calls, FakeDriver.fail_on = [], None
    monkeypatch.setitem(ac.DRIVERS, "hermes-window", FakeDriver)
    return FakeDriver


def test_a_run_goes_step_by_step_in_the_named_app(fake):
    out = ac.run("hermes", ac.plan("new session then prompt: say READY"), rows=ROWS)
    assert fake.calls == ["new", "say READY"]
    assert out == 'Started a new Hermes session.\nHermes, to "say READY": READY'


def test_a_failed_step_stops_the_rest(fake):
    fake.fail_on = "first"
    out = ac.run("hermes", ac.plan("prompt: first then prompt: second"), rows=ROWS)
    assert fake.calls == [] and out == "I stopped: the focus left Hermes, so I stopped before typing."


def test_unknown_and_not_yet_drivable_apps_say_so(fake):
    assert ac.run("excel", [{"op": "send", "text": "x"}], rows=ROWS) == "Nothing called /excel is running, sir."
    assert ac.run("chrome", [{"op": "send", "text": "x"}], rows=ROWS).startswith("I can't drive Google Chrome yet")
    assert fake.calls == []


def test_one_command_at_a_time(fake):
    assert ac._run_lock.acquire(blocking=False)
    try:
        assert ac.run("hermes", [{"op": "send", "text": "x"}], rows=ROWS).startswith("Another app command")
    finally:
        ac._run_lock.release()
    assert fake.calls == []
