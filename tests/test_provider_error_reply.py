"""A provider error 9router hands back as the reply is a failed model, not an answer.

9router answers HTTP 200 with the upstream error as the message content:
"[qoder error 403: {... isQueued ... retryAfterSeconds: 30 ...}]" with
finish_reason "stop" (2026-09-29). Both turn paths took it as the reply and
showed it to the user; the next model (Google) was never asked.
"""

import types

import pytest

from cygnus.brain import agent_loop as al
from cygnus.brain import google_ai
from cygnus.brain import router_health

QODER = ('\n[qoder error 403: {"code":"403","message":"{\\"code\\":\\"10605\\",\\"message\\":'
         '\\"{\\\\\\"isQueued\\\\\\":true,\\\\\\"modelKey\\\\\\":\\\\\\"qfmodel\\\\\\",'
         '\\\\\\"retryAfterSeconds\\\\\\":30,\\\\\\"se...]')


@pytest.mark.parametrize("text", [
    QODER,
    "[qoder error 403: queued]",
    "[kiro error 500: upstream]",
    "[antigravity/claude-sonnet-4-6] Unavailable (reset after 69h 3m 34s)",
    "[codex/gpt-5.5] [429]: The usage limit has been reached (reset after 24m 30s)",
    "[qoder/qfmodel] [502]: fetch failed (cause: ECONNRESET: read ECONNRESET)",
])
def test_a_provider_error_is_recognised(text):
    assert router_health.provider_error(text)


@pytest.mark.parametrize("text", [
    None, "", "It is 10:58 AM, sir.",
    "An error 403 means the server refused the request.",
    "Error handling in Python uses try and except.",
    "[Held] That changes files: say confirm.",
    "[NEEDS_CONFIRM:659ae83d] Say confirm to run it.",
    "Here is the log line: [qoder error 403: queued]",
])
def test_an_answer_is_not_an_error(text):
    assert router_health.provider_error(text) is None


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setattr(google_ai, "GOOGLE_AI_API_KEY", "test-key")
    monkeypatch.setattr(google_ai, "GOOGLE_AI_MODELS", ["gemini-a"])
    google_ai._reset_for_tests()
    yield
    google_ai._reset_for_tests()


def _reply(content):
    msg = types.SimpleNamespace(content=content, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def _api(create):
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def test_the_agent_loop_passes_an_error_reply_on_to_the_next_model(google, monkeypatch):
    from cygnus.brain import core as bg
    monkeypatch.setattr(bg, "_router_health",
                        lambda: types.SimpleNamespace(healthy_models=lambda: ["qd/qfmodel"]))
    monkeypatch.setattr(bg, "_router_models", lambda: ["qd/qfmodel"])
    monkeypatch.setattr(bg, "_router_ready", lambda: None)
    unhealthy = []
    monkeypatch.setattr(bg, "_report_unhealthy", lambda m, r: unhealthy.append((m, r)))
    monkeypatch.setattr(al, "_system", lambda ctx: "system")
    monkeypatch.setattr(al, "_make_client", lambda: _api(lambda model, **kw: _reply(QODER)))
    monkeypatch.setattr(google_ai, "client", lambda: _api(lambda model, **kw: _reply("I'm well, sir.")))

    out = al.run(types.SimpleNamespace(conversation=[]), "how are you?", {"client": "t"})

    assert out == "I'm well, sir."
    assert al._last_stats["model"] == "google/gemini-a"
    assert unhealthy and unhealthy[0][0] == "qd/qfmodel" and "qoder error 403" in unhealthy[0][1]
