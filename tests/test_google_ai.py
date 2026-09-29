"""Google AI (Cygnus's own Gemini key) as a model source of the agent loop."""

import types

import pytest

from cygnus.brain import agent_loop as al
from cygnus.brain import google_ai


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(google_ai, "GOOGLE_AI_API_KEY", "test-key")
    monkeypatch.setattr(google_ai, "GOOGLE_AI_MODELS", ["gemini-a", "gemini-b"])
    google_ai._reset_for_tests()
    yield
    google_ai._reset_for_tests()


def _health(monkeypatch, healthy, configured=("ag/claude", "ag/gemini")):
    from cygnus.brain import core as bg
    monkeypatch.setattr(bg, "_router_health",
                        lambda: types.SimpleNamespace(healthy_models=lambda: list(healthy)))
    monkeypatch.setattr(bg, "_router_models", lambda: list(healthy) or list(configured))


def test_no_key_means_no_google_models(monkeypatch):
    monkeypatch.setattr(google_ai, "GOOGLE_AI_API_KEY", "")
    assert google_ai.models() == []


def test_healthy_router_models_lead_google_follows(google, monkeypatch):
    _health(monkeypatch, ["ag/claude"])
    assert al._models() == ["ag/claude", "google/gemini-a", "google/gemini-b"]


def test_google_leads_when_9router_has_no_healthy_model(google, monkeypatch):
    _health(monkeypatch, [])
    assert al._models()[:2] == ["google/gemini-a", "google/gemini-b"]
    assert al._models()[2:] == ["ag/claude", "ag/gemini"]      # the failing ones, last


def test_a_failed_google_model_rests_and_quota_rests_longer(google):
    google_ai.mark_failed("google/gemini-a", "boom", now=100.0)
    assert google_ai.models(now=100.0 + google_ai.REST_S - 1) == ["google/gemini-b"]
    assert "google/gemini-a" in google_ai.models(now=100.0 + google_ai.REST_S + 1)
    google_ai.mark_failed("google/gemini-b", "Error code: 429 RESOURCE_EXHAUSTED", now=100.0)
    assert "google/gemini-b" not in google_ai.models(now=100.0 + google_ai.REST_S + 1)


def test_client_for_sends_google_models_to_google_by_their_own_name(google):
    router = object()
    api, wire = al._client_for("google/gemini-a", router)
    assert api is google_ai.client() and wire == "gemini-a"
    assert al._client_for("ag/claude", router) == (router, "ag/claude")


def test_a_google_failure_never_marks_9router_health(google, monkeypatch):
    from cygnus.brain import core as bg
    marked = []
    monkeypatch.setattr(bg, "_report_unhealthy", lambda m, r: marked.append(m))
    al._failed("google/gemini-a", RuntimeError("429"))
    al._failed("ag/claude", RuntimeError("503"))
    assert marked == ["ag/claude"]
    assert "google/gemini-a" not in google_ai.models()


def _reply(content=None):
    msg = types.SimpleNamespace(content=content, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_turn_answered_by_google_after_9router_models_fail(google, monkeypatch):
    from cygnus.brain import core as bg
    _health(monkeypatch, [])
    monkeypatch.setattr(bg, "_router_ready", lambda: None)
    monkeypatch.setattr(bg, "_report_unhealthy", lambda m, r: None)
    monkeypatch.setattr(al, "_system", lambda ctx: "system")
    calls = []

    def create_for(who):
        def create(model, **kw):
            calls.append((who, model))
            if who == "router":
                raise RuntimeError("503 Unavailable")
            return _reply("Opening Claude, sir.")
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    monkeypatch.setattr(al, "_make_client", lambda: create_for("router"))
    monkeypatch.setattr(google_ai, "client", lambda: create_for("google"))
    brain = types.SimpleNamespace(conversation=[])
    out = al.run(brain, "what time is it", {"client": "t"})
    assert out and "Claude" in out
    assert calls[0] == ("google", "gemini-a")        # Google first: 9router had no healthy model
    assert al._last_stats["model"] == "google/gemini-a"


def test_9router_down_turn_still_answered_by_google(google, monkeypatch):
    from cygnus.brain import core as bg
    _health(monkeypatch, [])

    def down():
        raise bg._RouterDown("9router is not running")
    monkeypatch.setattr(bg, "_router_ready", down)
    monkeypatch.setattr(al, "_system", lambda ctx: "system")
    seen = []

    def create(model, **kw):
        seen.append(model)
        return _reply("It is noon, sir.")
    monkeypatch.setattr(al, "_make_client", lambda: None)
    monkeypatch.setattr(google_ai, "client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))))
    out = al.run(types.SimpleNamespace(conversation=[]), "what time is it", {"client": "t"})
    assert out and seen == ["gemini-a"]


def test_tool_call_echo_keeps_geminis_thought_signature():
    fn = types.SimpleNamespace(name="open_app", arguments='{"name": "spotify"}')
    signed = types.SimpleNamespace(id="c1", function=fn,
                                   model_extra={"extra_content": {"google": {"thought_signature": "sig"}}})
    plain = types.SimpleNamespace(id="c2", function=fn, model_extra={})
    assert al._call_echo(signed)["extra_content"] == {"google": {"thought_signature": "sig"}}
    assert "extra_content" not in al._call_echo(plain)
    assert al._call_echo(plain)["function"] == {"name": "open_app", "arguments": '{"name": "spotify"}'}


def test_a_spent_daily_quota_rests_the_model_until_midnight_pacific(google):
    import datetime
    # 2026-09-27 09:00 at UTC-7 is 16:00 UTC: 15 h to midnight Pacific
    wall = datetime.datetime(2026, 9, 27, 16, 0, tzinfo=datetime.timezone.utc).timestamp()
    reason = "Error code: 429 - quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier"
    google_ai.mark_failed("google/gemini-a", reason, now=0.0, wall=wall)
    assert "google/gemini-a" not in google_ai.models(now=14.9 * 3600)
    assert "google/gemini-a" in google_ai.models(now=15.1 * 3600)
    assert google_ai.is_limit(reason) and not google_ai.is_limit("400 bad request")


def test_a_model_out_of_quota_mid_turn_hands_the_turn_to_the_next(google, monkeypatch):
    """The first model ran a tool, then hit its limit: the next one finishes the
    same conversation, and the tool is not run again."""
    from cygnus.brain import core as bg
    _health(monkeypatch, [])
    monkeypatch.setattr(bg, "_router_ready", lambda: None)
    monkeypatch.setattr(al, "_system", lambda ctx: "system")
    tools_run = []
    monkeypatch.setattr(al, "_run_tool", lambda name, args, ctx: tools_run.append(name) or "Chrome has 3 tabs open.")
    seen = {}

    def tool_reply():
        fn = types.SimpleNamespace(name="list_tabs", arguments='{"app": "chrome"}')
        tc = types.SimpleNamespace(id="c1", function=fn, model_extra={})
        msg = types.SimpleNamespace(content="", tool_calls=[tc])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    def create(model, messages, **kw):
        seen.setdefault(model, []).append(len(messages))
        if model == "gemini-a":
            if len(messages) == 2:
                return tool_reply()
            raise RuntimeError("Error code: 429 RESOURCE_EXHAUSTED GenerateRequestsPerDay")
        return _reply("Chrome has three tabs open, sir.")
    monkeypatch.setattr(al, "_make_client", lambda: None)
    monkeypatch.setattr(google_ai, "client", lambda: types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))))
    out = al.run(types.SimpleNamespace(conversation=[]), "how many tabs in chrome", {"client": "t"})
    assert tools_run == ["list_tabs"]                       # once, not again by the next model
    assert seen["gemini-b"] == [4]                          # it got the tool call and its result
    assert "three tabs" in out and "stopped answering" not in out
    assert "google/gemini-a" not in google_ai.models()      # resting until the quota resets
