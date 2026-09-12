"""Each Hermes job runs in a session of its own and leaves the warm session
alone; /redirect reaches the job that just ran (2026-09-13)."""

import pytest

import warm_harness as wh


@pytest.fixture
def hermes(monkeypatch):
    monkeypatch.setattr(wh, "_session_id", "s-warm")
    monkeypatch.setattr(wh, "_last_job_session", None)
    monkeypatch.setattr(wh, "_save_session", lambda sid: None)
    cmds = []

    def run(reply):
        monkeypatch.setattr(wh, "_run", lambda cmd, timeout: cmds.append(cmd) or (f"{reply}\nsession_id: s-job", "", 0))
    return cmds, run


def test_a_job_does_not_resume_the_shared_session(hermes):
    cmds, run = hermes
    run("Fixed the mic.")
    assert wh.warm_send("fix the mic", fresh=True).startswith("Fixed the mic.")
    assert "--resume" not in cmds[0] and "--pass-session-id" in cmds[0]
    assert wh._session_id == "s-warm" and wh._last_job_session == "s-job"


def test_a_jobs_long_result_is_kept_whole(hermes):
    _, run = hermes
    run("x" * 3000)
    assert len(wh.warm_send("summarize it", fresh=True)) >= 3000


def test_redirect_goes_to_the_job_that_just_ran(hermes):
    cmds, run = hermes
    run("Fixed the mic.")
    wh.warm_send("fix the mic", fresh=True)
    wh.warm_redirect("also check the send button")
    assert cmds[-1][cmds[-1].index("--resume") + 1] == "s-job"


def test_without_fresh_it_still_resumes_the_warm_session(hermes):
    cmds, run = hermes
    run("ok")
    wh.warm_send("say ok")
    assert cmds[0][cmds[0].index("--resume") + 1] == "s-warm"
