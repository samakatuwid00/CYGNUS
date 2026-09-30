"""The models a single call outside a turn may use (one_shot.py).

Memory distilling, the memory arbiter, "describe that window" and the
Apps-panel rules asked the router's URL directly: with CYGNUS_PROVIDER=gemini
every one failed, and under JARVIS_LOCAL_ONLY they still reached the cloud
(2026-09-30).
"""

import pytest

from cygnus import config
from cygnus.brain import google_ai, one_shot


@pytest.fixture
def setup(monkeypatch):
    def _set(router: bool, google_key: str = "", local_only: bool = False):
        monkeypatch.setattr(config, "JARVIS_USE_9ROUTER", router)
        monkeypatch.setattr(config, "JARVIS_LOCAL_ONLY", local_only)
        monkeypatch.setattr(google_ai, "GOOGLE_AI_API_KEY", google_key)
        monkeypatch.setattr(google_ai, "GOOGLE_AI_MODELS", ["gemini-a", "gemini-b"])
        monkeypatch.setattr(google_ai, "_resting", {})
    return _set


def _names(pairs):
    return [model for _, model, _ in pairs]


def test_the_router_first_then_google(setup):
    setup(router=True, google_key="AIza-test")
    assert _names(one_shot.candidates(["ag/x", "ag/x", "ag/y"], timeout=5)) == ["ag/x", "ag/y", "gemini-a", "gemini-b"]


def test_gemini_only_asks_google(setup):
    setup(router=False, google_key="AIza-test")
    pairs = one_shot.candidates(["ag/x"], timeout=5)
    assert _names(pairs) == ["gemini-a", "gemini-b"]
    assert "generativelanguage.googleapis.com" in str(pairs[0][0].base_url)


def test_local_only_asks_no_cloud_model(setup):
    setup(router=True, google_key="AIza-test", local_only=True)
    assert one_shot.candidates(["ag/x"], timeout=5) == []


def test_memory_distilling_raises_when_no_model_may_answer(setup):
    """The caller must not move its marker past a chunk nothing read."""
    from cygnus.memory import hygiene
    setup(router=False)
    with pytest.raises(RuntimeError, match="no model"):
        hygiene._llm_extract("user: I moved to Cebu")


def _fake_client(monkeypatch, replies):
    """openai.OpenAI stand-in: each create() pops the next reply; an
    Exception is raised. Records (model, kwargs)."""
    import openai
    asked = []

    class Client:
        def __init__(self, **kw):
            self.chat = type("C", (), {})()
            self.chat.completions = type("Q", (), {})()
            self.chat.completions.create = self._create

        def _create(self, model, **kw):
            asked.append((model, kw))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            msg = type("M", (), {"content": reply})()
            return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()
    monkeypatch.setattr(openai, "OpenAI", Client)
    return asked


def test_an_empty_reply_is_a_failure_and_google_is_asked_not_to_think(setup, monkeypatch):
    """Asked for one word in 3 tokens, gemini-2.5-flash spent them thinking
    and replied with nothing (2026-09-30)."""
    setup(router=False, google_key="AIza-test")
    asked = _fake_client(monkeypatch, ["", "update"])
    assert one_shot.complete([], [{"role": "user", "content": "x"}], timeout=5, max_tokens=3) == ("update", "gemini-b")
    assert [m for m, _ in asked] == ["gemini-a", "gemini-b"]
    assert all(kw.get("reasoning_effort") == "none" for _, kw in asked)


def test_a_failed_google_model_rests_and_a_down_router_is_skipped(setup, monkeypatch):
    setup(router=True, google_key="AIza-test")
    asked = _fake_client(monkeypatch, [RuntimeError("Connection error."),
                                       RuntimeError("429 RESOURCE_EXHAUSTED"), "fine"])
    assert one_shot.complete(["ag/x", "ag/y"], [], timeout=5) == ("fine", "gemini-b")
    assert [m for m, _ in asked] == ["ag/x", "gemini-a", "gemini-b"]      # ag/y: the router is down
    assert "reasoning_effort" not in asked[0][1]                           # never sent to the router
    assert google_ai.models() == ["google/gemini-b"]


def test_nothing_usable_raises_with_every_reason(setup, monkeypatch):
    setup(router=False, google_key="AIza-test")
    _fake_client(monkeypatch, ["not json", RuntimeError("503")])
    with pytest.raises(RuntimeError, match="gemini-a: unusable reply; gemini-b: RuntimeError"):
        one_shot.complete([], [], timeout=5, accept=lambda t: t.startswith("{"))
