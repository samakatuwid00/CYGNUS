"""An instant answer (time, maths, a greeting) never waits on the model that
reads Apps-panel rules by meaning: that call made "what time is it" take
about 4 s (smoke run, 2026-09-22)."""

import brain_gemini
import rules_match
import tools


def test_try_rule_action_can_skip_the_semantic_tier(monkeypatch):
    asked = []
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: asked.append(t))
    assert tools.try_rule_action("it's gym time now", semantic=False) is None
    assert asked == []
    tools.try_rule_action("it's gym time now")
    assert asked == ["it's gym time now"]


def test_an_instant_turn_skips_it(monkeypatch):
    import agent_loop
    monkeypatch.setattr(agent_loop, "MODE", "off")
    seen = []
    real = tools.try_rule_action
    monkeypatch.setattr(tools, "try_rule_action", lambda t, semantic=True: seen.append(semantic) or real(t, semantic=semantic))
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: seen.append("model"))
    b = brain_gemini.JarvisBrain()
    assert b.think("what time is it").startswith("It is ")
    assert b.think("17 * 23") == "That's 391, sir."
    assert seen == [False, False]
