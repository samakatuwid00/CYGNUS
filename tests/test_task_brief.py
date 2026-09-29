"""What a delegated agent is told: the task restated and first, the owner's
words kept, a rephrasing that drops a constraint distrusted, lead-in fillers
gone, and vault hits below the score floor left out."""

import pytest

from cygnus.brain import context_assembler as ca
from cygnus.brain import task_brief as tb
from cygnus import tools


# ------------------------------------------------------------- restate() --

def test_without_a_model_the_words_as_spoken_go_minus_fillers():
    assert tb.restate("okay jarvis, fix the mic button", "okay jarvis, fix the mic button") == "fix the mic button"


def test_a_model_brief_is_task_then_constraints_then_open_questions():
    ask = lambda prompt: ({"task": "Align the send and mic buttons in the terminal chat.",
                           "constraints": ["don't touch the tests"],
                           "unclear": ["which panel"]}, "m")
    out = tb.restate("fix the buttons, don't touch the tests", ask=ask)
    assert out.startswith("Align the send and mic buttons")
    assert "Constraints:\n- don't touch the tests" in out
    assert "Unclear (ask before guessing):\n- which panel" in out


def test_a_model_that_drops_a_constraint_is_overruled_by_the_words():
    ask = lambda prompt: ({"task": "Align the buttons in the terminal chat.", "constraints": []}, "m")
    raw = "fix the buttons in chat.html, don't touch the tests"
    assert tb.restate(raw, raw, ask=ask) == raw


def test_the_model_sees_both_the_draft_and_the_words():
    seen = []
    ask = lambda prompt: seen.append(prompt) or (None, "no")
    tb.restate("tidy the README", "hey cygnus, delegate this to claude: tidy the README", ask=ask)
    assert "delegate this to claude" in seen[0] and "assistant's draft" in seen[0]


@pytest.mark.parametrize("bad", [None, {}, {"task": "ok"}, "not json"])
def test_a_useless_model_answer_falls_back(bad):
    assert tb.restate("fix the mic button", ask=lambda p: (bad, "m")) == "fix the mic button"


def test_a_model_error_falls_back():
    def boom(prompt):
        raise RuntimeError("router down")
    assert tb.restate("fix the mic button", ask=boom) == "fix the mic button"


# -------------------------------------------------- dropped_constraints() --

@pytest.mark.parametrize("raw, task, lost", [
    ("fix the chat box, don't touch the tests", "Fix the chat box.", ["don't touch the tests"]),
    ("fix the chat box, don't touch the tests", "Fix the chat box; do not modify the tests.", []),
    ("fix the chat box, don't touch the tests", "Fix the chat box and update the tests.", ["don't touch the tests"]),
    ("keep the phone features", "Rework the sidebar.", ["keep the phone features"]),
    ("keep the phone features", "Rework the sidebar and keep the phone features.", []),
    ("edit web/jarvis_hud_v3.html only", "Edit the HUD page.", ["web/jarvis_hud_v3.html"]),
    ("edit web/jarvis_hud_v3.html only", "Edit web/jarvis_hud_v3.html.", []),
    ("make it 3 columns", "Make the grid three columns.", ["3"]),
    ('rename it to "Cygnus HUD"', "Rename the window.", ["Cygnus HUD"]),
    ("the buttons are not quite aligned", "Fix the misaligned buttons.", []),
    ("", "anything", []),
])
def test_what_a_rephrasing_lost(raw, task, lost):
    assert tb.dropped_constraints(raw, task) == lost


# -------------------------------------------------------------- compose() --

def test_the_task_comes_first_and_the_background_last():
    out = tb.compose("Fix the mic button.", "okay, fix the mic button", "CONTEXT:\nInstructions: x", "[PROFILE]\nowner")
    assert out.index("TASK:") < out.index("AS SPOKEN") < out.index(tb.BACKGROUND_HEAD) < out.index("[PROFILE]")
    assert "okay, fix the mic button" in out


def test_identical_words_are_not_repeated():
    out = tb.compose("fix the mic", "fix the mic", "", "")
    assert out == "TASK:\nfix the mic\n"


# ------------------------------------------------ compose_prompt() wiring --

@pytest.fixture
def quiet_brief(monkeypatch):
    monkeypatch.setattr(ca, "_load_profile", lambda: "owner likes dark mode")
    monkeypatch.setattr(ca, "_recent_summary", lambda n: "")
    monkeypatch.setattr(ca, "_app_context", lambda t: "")
    monkeypatch.setattr(ca, "_vault_context", lambda t, max_hits=3, delegate=None: "")


def test_claude_gets_the_code_instructions_then_the_task_then_the_background(quiet_brief):
    out = ca.compose_prompt("fix the chat box", delegate="claude", raw_task="delegate this to claude: fix the chat box")
    assert out.startswith("CONTEXT for CODE delegate (claude-cli):\nInstructions:")
    assert out.index("TASK:\nfix the chat box") < out.index("AS SPOKEN") < out.index("owner likes dark mode")


def test_hermes_keeps_its_grounding_rule_first(quiet_brief):
    out = ca.compose_prompt("what is on my calendar", delegate="hermes")
    assert out.startswith("CONTEXT (JARVIS persistent memory):\nGround factual answers")
    assert "TASK:\nwhat is on my calendar" in out and "AS SPOKEN" not in out


def test_the_old_brief_layout_is_unchanged_for_specialists(quiet_brief):
    out = ca.assemble_brief("build a todo app", delegate="opencode")
    assert out.startswith("CONTEXT for CODE delegate (opencode):\n[PROFILE]") and out.endswith("TASK:\n")


def test_a_cli_job_runs_the_composed_prompt(monkeypatch, tmp_path):
    from cygnus.jobs import registry as jobs
    monkeypatch.setattr(jobs, "_LOG_PATH", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", True)
    monkeypatch.setattr(jobs, "_listeners", [])
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(ca, "compose_prompt",
                        lambda task, delegate=None, raw_task=None: f"COMPOSED[{delegate}] {task} <- {raw_task}")
    got, heard = [], []
    monkeypatch.setattr(tools, "_cli_installed", lambda name: True)
    monkeypatch.setattr(tools, "_cli_command", lambda prompt, name: got.append(prompt) or None)
    ack = tools._start_cli_job("fix the mic", "claude", raw_task="delegate this to claude: fix the mic",
                               on_done=heard.append)
    assert ack.startswith("⟳ HERMES_BACKGROUND:")           # before the brief is written
    import time
    for _ in range(200):
        if heard:
            break
        time.sleep(.02)
    assert got == ["COMPOSED[claude] fix the mic <- delegate this to claude: fix the mic"]
    assert heard and heard[0].startswith("[Error]")           # the fake command was None
    (job,) = jobs.recent(5)
    assert job["state"] == "error"


def test_a_missing_cli_is_refused_at_once(monkeypatch):
    monkeypatch.setattr(tools, "_cli_installed", lambda name: False)
    out = tools._start_cli_job("fix the mic", "claude")
    assert out.startswith("[Error]") and "not installed" in out


# ------------------------------------------------- the agent loop's check --

@pytest.fixture
def cli_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "_cli_delegate",
                        lambda task, agent, raw_task=None, on_done=None: calls.append((agent, task, raw_task)) or "ok")
    return calls


def test_the_models_task_is_used_when_it_kept_the_constraints(cli_calls):
    from cygnus.brain import agent_loop as al
    al._run_tool("delegate", {"agent": "claude", "task": "Align the chat buttons; do not touch the tests."},
                 {"user_text": "fix the chat buttons, don't touch the tests, use claude"})
    assert cli_calls == [("claude", "Align the chat buttons; do not touch the tests.",
                          "fix the chat buttons, don't touch the tests, use claude")]


def test_the_owners_words_replace_a_task_that_dropped_one(cli_calls):
    from cygnus.brain import agent_loop as al
    al._run_tool("delegate", {"agent": "claude", "task": "Align the chat buttons."},
                 {"user_text": "okay, fix the chat buttons, don't touch the tests, use claude"})
    (agent, task, raw) = cli_calls[0]
    assert agent == "claude" and task == "fix the chat buttons, don't touch the tests"
    assert raw == "okay, fix the chat buttons, don't touch the tests, use claude"


# ------------------------------------------------------ the vault floor --

def test_low_scoring_vault_hits_are_left_out(monkeypatch):
    monkeypatch.setattr(tools, "search_vault_semantic",
                        lambda q, limit=3: f"Semantic search found 2 results for '{q}': "
                                           "router.md (score=0.1200): the 9router | chat.md (score=0.6100): the chat box")
    out = ca._vault_context("fix the chat box", delegate="claude-cli")
    assert "chat.md" in out and "router.md" not in out


def test_a_vault_with_nothing_close_adds_no_block(monkeypatch):
    monkeypatch.setattr(tools, "search_vault_semantic",
                        lambda q, limit=3: f"Semantic search found 1 results for '{q}': router.md (score=0.1200): x")
    assert ca._vault_context("fix the chat box") == ""


def test_the_substring_fallback_has_no_scores_and_is_kept(monkeypatch):
    monkeypatch.setattr(tools, "search_vault_semantic", lambda q, limit=3: "Found 1 note: chat.md: the chat box")
    assert "chat.md" in ca._vault_context("fix the chat box")


# ------------------------------------------------------------ fillers --

@pytest.mark.parametrize("text, cleaned", [
    ("okay jarvis, delegate this to claude: fix the mic button", "fix the mic button"),
    ("no, I meant hand it off to codex: tidy the README", "tidy the README"),
    ("have Claude Code fix the chat box", "fix the chat box"),
])
def test_a_named_handoff_loses_its_lead_in(text, cleaned):
    forced, name, got = tools._explicit_cli_agent(text)
    assert forced and got == cleaned
