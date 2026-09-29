"""What the incident watcher counts as something going wrong (cygnus/incidents.py).

The replies below are the ones Cygnus gave on 2026-09-29, when six turns
failed in an hour and nothing was written down where a person would look.
"""

import json

import pytest

from cygnus import incidents
from cygnus.brain import core as b


def _turn(**kw):
    rec = {"user": "hi", "reply": "Hello, sir.", "backend": "agent", "seconds": 3.0, "tools": []}
    rec.update(kw)
    return rec


def _kinds(rec):
    return [p["kind"] for p in incidents.turn_problems(rec)]


def test_a_good_turn_is_not_an_incident():
    assert incidents.turn_problems(_turn()) == []


@pytest.mark.parametrize("rec,kind", [
    (_turn(backend="no-model", reply="My model didn't answer in time, sir. Please try again in a moment."),
     "no_model"),
    (_turn(reply="Opened the tab \u201cFacebook\u201d in Google Chrome. Then my model stopped answering, sir."),
     "model_stopped"),
    (_turn(reply='[qoder error 403: {"code":"10605","message":"isQueued"}]'), "error_reply"),
    (_turn(reply="[Tool Error] play_music: TimeoutError"), "error_reply"),
    (_turn(reply=""), "empty_reply"),
    (_turn(reply=None, error="KeyError: 'tool_call_id'"), "turn_crashed"),
])
def test_a_failed_turn_is_an_incident(rec, kind):
    assert _kinds(rec)[0] == kind


def test_a_slow_turn_is_one_even_when_it_answered():
    rec = _turn(seconds=66.9, tools=["search_web", "read_window", "list_tabs"])
    problems = incidents.turn_problems(rec)
    assert [p["kind"] for p in problems] == ["slow_turn"]
    assert "search_web" in problems[0]["detail"]


def test_a_reply_that_only_talks_about_errors_is_not_one():
    assert _kinds(_turn(reply="An error 403 means the server refused the request, sir.")) == []


def test_a_model_failing_inside_a_turn_that_answered_is_not_an_incident():
    # A dead 9router model fails every turn for days; the next one answers.
    scan = incidents.LogScanner()
    line = "[agent] ag/claude-sonnet-4-6 failed: InternalServerError: Error code: 503"
    assert scan.feed(line) == []
    assert incidents.turn_problems(_turn(broke="InternalServerError")) == []


def test_a_traceback_is_one_incident_named_by_its_exception():
    scan = incidents.LogScanner()
    lines = ["Traceback (most recent call last):",
             '  File "cygnus/server/app.py", line 1341, in ws',
             "    await send_telemetry(ws)",
             "RuntimeError: Cannot call \"send\" once a close message has been sent."]
    found = [p for line in lines for p in scan.feed(line)]
    assert len(found) == 1 and found[0]["kind"] == "traceback"
    assert found[0]["detail"].startswith("RuntimeError")
    assert len(found[0]["traceback"]) == 4


def test_a_chained_traceback_waits_for_the_last_exception():
    scan = incidents.LogScanner()
    lines = ["Traceback (most recent call last):", '  File "a.py", line 1, in f',
             "ValueError: first", "", "During handling of the above exception, another exception occurred:",
             "", "Traceback (most recent call last):", '  File "b.py", line 2, in g', "KeyError: 'x'"]
    found = [p for line in lines for p in scan.feed(line)]
    # The first exception line ends the first report; the chained one is its own.
    assert [p["detail"] for p in found] == ["ValueError: first", "KeyError: 'x'"]


@pytest.mark.parametrize("line,kind", [
    ("[agent] failed (KeyError: 'x'); old routing", "agent_crashed"),
    ("[WS] error: WebSocket is not connected. Need to call \"accept\" first.", "websocket_error"),
    ("Exception in ASGI application", "server_error"),
])
def test_log_lines_that_are_problems(line, kind):
    assert incidents.LogScanner().feed(line)[0]["kind"] == kind


def test_polling_is_noise():
    assert incidents.NOISE_RE.search('INFO:     127.0.0.1:64294 - "GET /status HTTP/1.1" 200 OK')
    assert not incidents.NOISE_RE.search("[agent] whats_running {} -> 'Agents: ...'")


def test_the_same_problem_again_has_the_same_fingerprint():
    a = {"kind": "no_model", "detail": "It resets in 33h 34m."}
    c = {"kind": "no_model", "detail": "It resets in 12h 1m."}
    assert incidents.fingerprint(a) == incidents.fingerprint(c)


def test_every_turn_leaves_a_record(tmp_path, monkeypatch):
    path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(incidents, "TURNS_PATH", str(path))
    brain = object.__new__(b.JarvisBrain)
    brain.last_stats, brain.last_backend = {"backend": "agent", "model": "google/gemini-a"}, "agent"
    brain._think_observed = lambda *a: "Hello, sir."
    assert brain.think("hi", client="phone") == "Hello, sir."
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["user"] == "hi" and rec["reply"] == "Hello, sir." and rec["client"] == "phone"
    assert rec["model"] == "google/gemini-a" and rec["seconds"] >= 0


def test_a_turn_that_raises_is_recorded_and_still_raises(tmp_path, monkeypatch):
    path = tmp_path / "turns.jsonl"
    monkeypatch.setattr(incidents, "TURNS_PATH", str(path))
    brain = object.__new__(b.JarvisBrain)
    brain.last_stats, brain.last_backend = {}, None

    def boom(*a):
        raise KeyError("tool_call_id")
    brain._think_observed = boom
    with pytest.raises(KeyError):
        brain.think("hi")
    rec = json.loads(path.read_text(encoding="utf-8"))
    assert rec["reply"] is None and rec["error"].startswith("KeyError")
    assert _kinds(rec) == ["turn_crashed"]


def test_a_record_that_cannot_be_written_never_breaks_the_turn(monkeypatch):
    monkeypatch.setattr(incidents, "TURNS_PATH", "Z:/no/such/drive/turns.jsonl")
    incidents.record_turn("hi", "Hello", {}, "agent", 1.0)       # no exception


def test_a_client_going_away_is_not_a_traceback_worth_reporting():
    scan = incidents.LogScanner()
    lines = ["Traceback (most recent call last):",
             '  File "asyncio/proactor_events.py", line 165, in _call_connection_lost',
             "ConnectionResetError: [WinError 10054] An existing connection was forcibly closed by the remote host"]
    assert [p for line in lines for p in scan.feed(line)] == []
