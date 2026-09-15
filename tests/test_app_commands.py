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


# ------------------------------------------------ reading the intent --

PPT_ROW = {"id": "20", "name": "PowerPoint", "group": "apps", "doing": "Deck",
           "windows": [{"hwnd": 20, "title": "Deck - PowerPoint"}], "exes": ["powerpnt.exe"]}
APPS = {"powerpnt": {"bin": r"C:\Office\POWERPNT.EXE", "abilities": [
            {"id": "powerpnt.save", "name": "Save", "level": "ask", "needs": {}},
            {"id": "powerpnt.find", "name": "Find in the document", "level": "safe", "needs": {}}]},
        "powerpoint": {"bin": r"C:\Office\POWERPNT.EXE", "abilities": [
            {"id": "powerpoint.click_new_slide", "name": "Click \u201cNew Slide\u201d", "level": "safe"},
            {"id": "powerpoint.click_bold", "name": "Click \u201cBold\u201d", "level": "safe"},
            {"id": "powerpoint.hidden", "name": "Never", "level": "never"}]}}
HERMES_ROWS = [row("13", "Hermes desktop app", group="agents")]


def model_says(monkeypatch, data):
    seen = []
    monkeypatch.setattr(ac, "_ask_model", lambda prompt: (seen.append(prompt), (data, "cx/gpt-5.6-luna"))[1])
    return seen


def test_powerpoint_is_drivable_through_its_abilities():
    t = ac.targets([PPT_ROW], APPS)
    assert t == [{"slug": "powerpoint", "row": "20", "name": "PowerPoint", "doing": "Deck", "kind": "window",
                  "driver": "abilities"}]
    acts = ac.actions(t[0], PPT_ROW, APPS)
    assert [a["does"] for a in acts] == ["Save", "Find in the document", "Click \u201cNew Slide\u201d",
                                         "Click \u201cBold\u201d"]         # "never" is left out


def test_hermes_reads_the_intent_not_the_words(monkeypatch):
    prompt = model_says(monkeypatch, {"steps": [
        {"do": "open_session", "args": {"name": "Musubae status check"}},
        {"do": "send", "args": {"text": "any update?"}}], "missing": ""})
    got = ac.understand("hermes", "go to my musubae chat and ask for an update", rows=HERMES_ROWS, apps={},
                        sessions=["Reply with PONG", "Musubae status check"])
    assert got["steps"] == [{"op": "open_session", "name": "Musubae status check"},
                            {"op": "send", "text": "any update?"}]
    assert "Musubae status check" in prompt[0] and "go to my musubae chat" in prompt[0]


def test_what_the_app_cannot_do_is_said_with_what_it_can(monkeypatch):
    model_says(monkeypatch, {"steps": [], "missing": "next slide"})
    got = ac.understand("powerpoint", "next slide", rows=[PPT_ROW], apps=APPS)
    assert got["steps"] == [] and got["missing"] == "next slide"
    reply = ac.cannot_reply(got["target"], got["missing"], got["actions"])
    assert reply.startswith("I can't do \u201cnext slide\u201d in PowerPoint, sir: it has no action for that.")
    assert "add a rule for PowerPoint in the Apps panel" in reply


def test_the_refusal_names_actions_but_only_counts_scanned_buttons():
    acts = [{"do": "ability:n:save", "does": "Save"}, {"do": "ability:n:find", "does": "Find"},
            {"do": "ability:n:tab", "does": "Click \u201cHost 76.13.23.174. Modified.\u201d", "scanned": True},
            {"do": "ability:n:tab2", "does": "Click \u201cschool. Modified.\u201d", "scanned": True},
            {"do": "rule:n:r1", "does": "your rule: next page \u2192 Page Down"}]
    reply = ac.cannot_reply({"name": "Notepad"}, "next slide", acts)
    assert "What I can do there: Save; Find; and 2 buttons found in its window." in reply
    assert "76.13.23.174" not in reply and "Your rules for it: your rule: next page \u2192 Page Down." in reply


def test_an_invented_action_counts_as_missing(monkeypatch):
    model_says(monkeypatch, {"steps": [{"do": "ability:powerpoint:next_slide", "args": {}}], "missing": ""})
    got = ac.understand("powerpoint", "next slide", rows=[PPT_ROW], apps=APPS)
    assert got["steps"] == [] and got["missing"] == "next slide"


def test_an_ability_step_keeps_its_confirm_level(monkeypatch):
    model_says(monkeypatch, {"steps": [{"do": "ability:powerpnt:powerpnt.save", "args": {"junk": 1}}], "missing": ""})
    got = ac.understand("powerpoint", "save it", rows=[PPT_ROW], apps=APPS)
    assert got["steps"] == [{"op": "ability", "app": "powerpnt", "id": "powerpnt.save", "args": {},
                             "label": "Save", "ask": True}]
    assert ac.describe(got["steps"][0]) == "Save"


def test_without_a_model_hermes_falls_back_to_phrases(monkeypatch):
    monkeypatch.setattr(ac, "_ask_model", lambda prompt: (None, "no model"))
    got = ac.understand("hermes", "new session then prompt: hi", rows=HERMES_ROWS, apps={}, sessions=[])
    assert got["by"] == "phrases" and got["steps"] == [{"op": "new_session"}, {"op": "send", "text": "hi"}]
    ppt = ac.understand("powerpoint", "next slide", rows=[PPT_ROW], apps=APPS)
    assert ppt.get("no_model") and ac.cannot_reply(ppt["target"], "", [], True).startswith("I need my model")


@pytest.mark.parametrize("name, title", [
    ("my musubae chat", "Musubae status check"), ("Check second brain vault", "Check second brain vault staleness"),
    ("the PONG one", "Reply with PONG"), ("excel budget", None)])
def test_a_spoken_session_name_finds_its_title(name, title):
    titles = ["Reply with PONG", "Musubae status check", "Check second brain vault staleness"]
    assert ac.best_title(name, titles) == title


def test_an_ability_step_runs_through_the_voice_routers_runner(monkeypatch):
    import intent_router
    ran = []
    monkeypatch.setattr(intent_router, "_run_ability",
                        lambda app, aid, args, apps: (ran.append((app, aid)), ("Find in the document, sir (PowerPoint).", True))[1])
    step = {"op": "ability", "app": "powerpnt", "id": "powerpnt.find", "args": {}, "label": "Find in the document"}
    assert ac.run("powerpoint", [step], rows=[PPT_ROW], apps=APPS) == "Find in the document, sir (PowerPoint)."
    assert ran == [("powerpnt", "powerpnt.find")]


def test_one_command_at_a_time(fake):
    assert ac._run_lock.acquire(blocking=False)
    try:
        assert ac.run("hermes", [{"op": "send", "text": "x"}], rows=ROWS).startswith("Another app command")
    finally:
        ac._run_lock.release()
    assert fake.calls == []
