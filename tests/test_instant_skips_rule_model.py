"""An instant answer (time, maths, a greeting) never waits on the model that
reads Apps-panel rules by meaning: that call made "what time is it" take
about 4 s (smoke run, 2026-09-22)."""

import pytest

from cygnus.brain import core as brain_gemini
from cygnus.rules import match as rules_match
from cygnus import tools


def test_try_rule_action_can_skip_the_semantic_tier(monkeypatch):
    asked = []
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: asked.append(t))
    assert tools.try_rule_action("it's gym time now", semantic=False) is None
    assert asked == []
    tools.try_rule_action("it's gym time now")
    assert asked == ["it's gym time now"]


def test_an_instant_turn_skips_it(monkeypatch):
    from cygnus.brain import agent_loop
    monkeypatch.setattr(agent_loop, "MODE", "off")
    seen = []
    real = tools.try_rule_action
    monkeypatch.setattr(tools, "try_rule_action", lambda t, semantic=True: seen.append(semantic) or real(t, semantic=semantic))
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: seen.append("model"))
    b = brain_gemini.JarvisBrain()
    assert b.think("what time is it").startswith("It is ")
    assert b.think("17 * 23") == "That's 391, sir."
    assert seen == [False, False]


@pytest.mark.parametrize("say, semantic", [
    ("stop the music", False),          # instant: answered without a model
    ("what's the status of the task?", False),
    ("what do you remember about me?", False),
    ("play some music", True),          # a rule may mean this ("play music brave")
    ("it's gym time", True),            # a rule by meaning: the tier earns its 3.5 s
])
def test_only_the_deterministic_intents_skip_the_rule_model(say, semantic, monkeypatch):
    # "stop the music" spent 3.5 s on the rule model with nothing playing
    # (bench/latency.py, 2026-09-23).
    from cygnus.brain import agent_loop as al
    monkeypatch.setattr(al, "MODE", "off")
    seen = []
    real = tools.try_rule_action
    monkeypatch.setattr(tools, "try_rule_action",
                        lambda t, semantic=True: seen.append(semantic) or real(t, semantic=semantic))
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: seen.append("model"))
    brain_gemini.JarvisBrain().think(say)
    assert seen and seen[0] is semantic, f"{say}: rule model {'skipped' if semantic else 'used'}"
