"""A rule guessed by meaning is not offered when the words name another
app: "play lo-fi on spotify" was answered "Did you mean your 'play music
brave' rule (brave)?" (live smoke, 2026-09-22)."""

from cygnus.rules import match as rules_match


def _fake(monkeypatch, owner="brave"):
    rule = {"rule_id": "r1", "name": "play music brave"}
    monkeypatch.setattr(rules_match, "catalogue", lambda apps=None, front=None: [(owner, rule, {}, {})])
    monkeypatch.setattr(rules_match, "_context", lambda: "")
    monkeypatch.setattr(rules_match, "_known_apps", lambda: {"brave", "spotify", "chrome", "notepad"})
    return lambda prompt: ({"rule": f"{owner}/r1", "confidence": 0.9}, "fake")


def test_a_rule_for_another_app_is_not_guessed(monkeypatch):
    llm = _fake(monkeypatch)
    assert rules_match.semantic("play lo-fi on spotify", llm=llm, front="") is None


def test_the_rules_own_app_or_no_app_still_matches(monkeypatch):
    llm = _fake(monkeypatch)
    assert rules_match.semantic("play some lo-fi in brave", llm=llm, front="")["owner"] == "brave"
    assert rules_match.semantic("play some lo-fi music", llm=llm, front="")["owner"] == "brave"
