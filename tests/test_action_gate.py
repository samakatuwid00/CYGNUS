"""action_gate: act when safe, confirm when risky, ask when there are several
real choices; in shadow it only logs what it would have done."""

import json
from types import SimpleNamespace

import pytest

from cygnus.brain import action_gate
from cygnus.brain import dialogue_state


@pytest.fixture(autouse=True)
def no_real_lookups(monkeypatch):
    from cygnus import tools
    monkeypatch.setattr(tools, "resolve_open_target", lambda t: (None, t))
    monkeypatch.setattr(action_gate, "_intake", lambda t: SimpleNamespace(
        entity=None, verb=None, candidates=[], needs_confirmation=lambda: False))
    dialogue_state.reset()
    yield
    dialogue_state.reset()


@pytest.mark.parametrize("text, risky", [
    ("delete the old logs folder", True), ("install python 3.13", True),
    ("fix the chat box in jarvis_hud_v3.html", True),
    ("open spotify", False), ("play some jazz", False), ("what's on my calendar", False)])
def test_risk_comes_from_the_confirm_gates_lists(text, risky):
    assert action_gate.is_risky(text) is risky


def test_safe_and_sure_acts_risky_and_sure_confirms():
    assert action_gate.decide("play some jazz").kind == "act"
    assert action_gate.decide("delete the old logs folder").kind == "confirm"


def test_an_app_and_a_website_with_one_name_is_asked_about(monkeypatch):
    from cygnus import tools
    monkeypatch.setattr(tools, "resolve_open_target", lambda t: ("clarify", {"name": "spotify"}))
    d = action_gate.decide("open spotify", open_target="spotify")
    assert d.kind == "clarify"
    assert d.question == "The spotify app or the website, sir?"
    assert d.options == ["open the spotify app", "open spotify website"]


def test_a_name_heard_badly_offers_the_candidates(monkeypatch):
    monkeypatch.setattr(action_gate, "_intake", lambda t: SimpleNamespace(
        entity="taylor swift", verb="play", candidates=["play", "taylor swift", "tyla"],
        needs_confirmation=lambda: True))
    d = action_gate.decide("play talor swif")
    assert d.kind == "clarify"
    assert d.question == "Did you mean taylor swift or tyla, sir?"
    assert d.options == ["play taylor swift", "play tyla"]


def test_unsure_without_choices_still_acts_and_says_why():
    d = action_gate.decide("play some jazz", semantic_score=0.65, router_confidence=0.7)
    assert d.kind == "act"
    assert d.reasons == ["semantic pick unsure (0.65)", "router unsure (0.70)"]


def test_asking_opens_a_clarify_the_answer_routes(monkeypatch):
    from cygnus import tools
    from cygnus.brain.core import answer_choice
    monkeypatch.setattr(tools, "resolve_open_target", lambda t: ("clarify", {"name": "spotify"}))
    d = action_gate.decide("open spotify", open_target="spotify")
    assert action_gate.ask(d) == "The spotify app or the website, sir?"
    assert answer_choice("the app") == (None, "open the spotify app")


def test_the_shadow_log_records_would_and_did(tmp_path, monkeypatch):
    log = tmp_path / "gate_shadow.jsonl"
    monkeypatch.setattr(action_gate, "SHADOW_LOG", str(log))
    action_gate.log_shadow("delete the old logs folder", action_gate.decide("delete the old logs folder"),
                           {"backend": "hermes-confirm", "asked": True})
    rec = json.loads(log.read_text(encoding="utf-8"))
    assert (rec["would"], rec["risky"], rec["ran"]["backend"]) == ("confirm", True, "hermes-confirm")


def test_shadow_is_the_default(monkeypatch):
    import importlib
    monkeypatch.delenv("JARVIS_GATE", raising=False)
    assert importlib.reload(action_gate).MODE == "shadow"
