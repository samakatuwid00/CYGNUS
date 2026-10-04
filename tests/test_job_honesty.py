"""A job is told as it went (2026-09-13): a changing task whose reply only
describes the work is not Done, and a delegation the model starts runs in
the background when the turn can take a late answer."""

import pytest

from cygnus.jobs import registry as jobs
from cygnus.brain import plain_reply
from cygnus import tools


@pytest.fixture
def hermes(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "_LOG_PATH", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", True)
    monkeypatch.setattr(jobs, "_listeners", [])
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL", None)
    monkeypatch.setitem(tools._PENDING_DESTRUCTIVE, "task", None)
    monkeypatch.setattr(tools, "_repo_changes", lambda root=None: {})

    def reply(text):
        monkeypatch.setattr(tools, "_run_hermes_sync", lambda *a, **k: text)
    return reply


def _run_confirmed(task):
    tools.delegate_to_hermes(task)
    tools.confirm_pending()
    return jobs.recent(1)[0]


def test_a_description_of_the_work_is_not_done(hermes):
    hermes("I would delegate this to Claude Code. The goal for Claude would be to fix the buttons.")
    job = _run_confirmed("fix the chat buttons")
    assert job["state"] == "unverified"
    card = plain_reply.card(job)
    assert card["label"] == "Not done" and "only described" in card["detail"]
    assert "wasn't done" in plain_reply.announce_job(job)


def test_work_that_changed_files_stays_done(hermes, monkeypatch):
    hermes("I would say it went well: the mic now sits on the text's midline.")
    snapshots = iter([{}, {"jarvis_hud_v3.html": 1.0}])
    monkeypatch.setattr(tools, "_repo_changes", lambda root=None: next(snapshots))
    assert _run_confirmed("fix the chat buttons")["state"] == "done"


def test_a_plain_report_of_work_done_stays_done(hermes):
    hermes("Fixed the mic alignment and checked it at panel size.")
    assert _run_confirmed("fix the chat buttons")["state"] == "done"


def test_a_look_only_answer_may_describe(hermes, monkeypatch):
    monkeypatch.setattr(tools, "_is_safe_task", lambda t: True)
    hermes("I would change the mic margin in jarvis_hud_v3.html.")
    tools.delegate_to_hermes("check the chat buttons")
    assert jobs.recent(1)[0]["state"] == "done"


def _capture_delegate(monkeypatch):
    got = {}

    def fake(task, backend=None, confirm=False, grounded=True, background=False,
             on_done=None, timeout=300, max_turns=15):
        got.update(background=background, on_done=on_done)
        return "ACK"
    monkeypatch.setattr(tools, "delegate", fake)
    return got


def test_the_models_delegate_runs_in_the_background_when_the_turn_allows(monkeypatch):
    got = _capture_delegate(monkeypatch)
    cb = lambda result: None                    # noqa: E731
    tools.set_job_callback(cb)
    try:
        tools.TOOL_MAP["delegate"](task="fix the chat buttons")
    finally:
        tools.set_job_callback(None)
    assert got == {"background": True, "on_done": cb}


def test_without_a_late_answer_path_it_stays_in_the_turn(monkeypatch):
    got = _capture_delegate(monkeypatch)
    tools.set_job_callback(None)
    tools.TOOL_MAP["delegate"](task="fix the chat buttons")
    assert got == {"background": False, "on_done": None}
