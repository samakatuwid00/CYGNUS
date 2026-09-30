"""CYGNUS_PROVIDER + CYGNUS_API_KEY: the two settings a student sets.

Each provider fills in the endpoint, the models and which backends are on; a
provider that cannot work says what to fix. Unset, config.py reads exactly as
it did before providers existed, so the author's 9router .env is unchanged.
"""

import importlib

import dotenv
import pytest

from cygnus import config
# Imported before any reload, so they keep the real settings for later tests.
from cygnus.brain import core, router_keeper

# Everything the provider presets read or fill in.
_VARS = ("CYGNUS_PROVIDER", "CYGNUS_API_KEY", "CYGNUS_MODEL", "JARVIS_USE_9ROUTER",
         "ROUTER_BASE_URL", "ROUTER_MODEL", "ROUTER_API_KEY", "ROUTER_FALLBACK_MODELS",
         "GOOGLE_AI_API_KEY", "GOOGLE_AI_MODELS", "JARVIS_USE_OLLAMA", "JARVIS_PREFER_LOCAL",
         "OLLAMA_MODEL", "GROQ_API_KEY", "CEREBRAS_API_KEY",
         "JARVIS_GROQ_KEY_PATH", "JARVIS_CEREBRAS_KEY_PATH")


@pytest.fixture
def load(monkeypatch):
    """config as a fresh process would read it from exactly these settings:
    the real .env is neither read nor left in the environment."""
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)

    def _load(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(config)

    yield _load
    monkeypatch.undo()
    importlib.reload(config)          # back to the real settings for later tests


def test_no_provider_keeps_the_9router_setup(load):
    c = load()
    assert c.PROVIDER_PROBLEM == ""
    assert c.JARVIS_USE_9ROUTER is True
    assert c.ROUTER_BASE_URL == "http://localhost:20128/v1"
    assert c.ROUTER_API_KEY == "dummy"
    assert c.JARVIS_USE_OLLAMA is True
    assert c.ROUTER_FALLBACK_MODELS[0] == "ag/gemini-3.5-flash-low"


def test_gemini_uses_the_key_directly_and_no_router(load):
    c = load(CYGNUS_PROVIDER="gemini", CYGNUS_API_KEY="AIza-test")
    assert c.PROVIDER_PROBLEM == ""
    assert c.GOOGLE_AI_API_KEY == "AIza-test"
    assert c.JARVIS_USE_9ROUTER is False
    assert c.JARVIS_USE_OLLAMA is False          # not installed on a student laptop
    assert c.ROUTER_API_KEY == "dummy"           # the key never goes to the router


def test_openai_fills_in_endpoint_model_and_key(load):
    c = load(CYGNUS_PROVIDER="OpenAI", CYGNUS_API_KEY="sk-test")
    assert c.PROVIDER_PROBLEM == ""
    assert c.JARVIS_USE_9ROUTER is True
    assert c.ROUTER_BASE_URL == "https://api.openai.com/v1"
    assert c.ROUTER_MODEL == "gpt-5.4-mini"
    assert c.ROUTER_API_KEY == "sk-test"
    # 9router's model names mean nothing to OpenAI.
    assert not any(m.startswith(("ag/", "cx/")) for m in c.ROUTER_FALLBACK_MODELS)


def test_a_setting_written_out_wins_over_the_provider(load):
    c = load(CYGNUS_PROVIDER="openai", CYGNUS_API_KEY="sk-test", ROUTER_MODEL="gpt-5.4")
    assert c.ROUTER_MODEL == "gpt-5.4"


def test_missing_key_is_said_plainly(load):
    c = load(CYGNUS_PROVIDER="gemini")
    assert "No API key" in c.PROVIDER_PROBLEM and "docs/API_KEY.md" in c.PROVIDER_PROBLEM


def test_unknown_provider_lists_the_real_ones(load):
    c = load(CYGNUS_PROVIDER="chatgpt", CYGNUS_API_KEY="x")
    assert "not a provider" in c.PROVIDER_PROBLEM and "gemini" in c.PROVIDER_PROBLEM


def test_openrouter_needs_a_model_named(load):
    c = load(CYGNUS_PROVIDER="openrouter", CYGNUS_API_KEY="sk-or-test")
    assert "CYGNUS_MODEL" in c.PROVIDER_PROBLEM
    c = load(CYGNUS_MODEL="some/model:free")
    assert c.PROVIDER_PROBLEM == "" and c.ROUTER_MODEL == "some/model:free"


def test_ollama_needs_no_key_and_goes_local(load):
    c = load(CYGNUS_PROVIDER="ollama")
    assert c.PROVIDER_PROBLEM == ""
    assert c.JARVIS_USE_9ROUTER is False
    assert c.JARVIS_USE_OLLAMA is True and c.JARVIS_PREFER_LOCAL is True
    assert c.OLLAMA_MODEL == "qwen3:4b-instruct"


def test_a_groq_key_alone_does_not_stop_cygnus(load):
    """Path was imported only in the Groq branch: a Groq key with no Cerebras
    key raised NameError at import (2026-09-30)."""
    c = load(GROQ_API_KEY="gsk-test")
    assert c.GROQ_API_KEY == "gsk-test" and c.CEREBRAS_API_KEY == ""


def test_the_no_model_reply_names_the_setup_problem(load, monkeypatch):
    load(CYGNUS_PROVIDER="gemini")
    assert "No API key" in core._why_no_model(RuntimeError("Connection error"))


def test_a_refused_key_is_named(load):
    load()
    reply = core._why_no_model(RuntimeError("Error code: 401 - Incorrect API key provided"))
    assert "refused the API key" in reply


def test_a_remote_endpoint_is_checked_on_its_own_port(monkeypatch):
    """https://api.openai.com/v1 was checked on 9router's port 20128, so a
    student's own endpoint always looked down (2026-09-30)."""
    monkeypatch.setattr(config, "ROUTER_BASE_URL", "https://api.openai.com/v1")
    assert router_keeper._address() == ("api.openai.com", 443)
    monkeypatch.setattr(config, "ROUTER_BASE_URL", "http://localhost:20128/v1")
    assert router_keeper._address() == ("localhost", 20128)


def test_a_refused_google_key_rests_every_model_and_is_the_reply(monkeypatch):
    """One key serves every Google model: a wrong one cost seven calls a turn
    and the reply said only "No model answered" (2026-09-30)."""
    from cygnus.brain import google_ai
    monkeypatch.setattr(google_ai, "GOOGLE_AI_API_KEY", "AIza-wrong")
    monkeypatch.setattr(google_ai, "GOOGLE_AI_MODELS", ["gemini-a", "gemini-b", "gemini-c"])
    monkeypatch.setattr(google_ai, "_resting", {})
    monkeypatch.setattr(google_ai, "last_error", "")
    google_ai.mark_failed("google/gemini-a", "Error code: 400 - Please pass a valid API key")
    assert google_ai.models() == []
    assert "refused the API key" in core._why_no_model(RuntimeError(google_ai.last_error))


def test_a_quota_refusal_rests_only_that_model(monkeypatch):
    from cygnus.brain import google_ai
    monkeypatch.setattr(google_ai, "GOOGLE_AI_API_KEY", "AIza-ok")
    monkeypatch.setattr(google_ai, "GOOGLE_AI_MODELS", ["gemini-a", "gemini-b"])
    monkeypatch.setattr(google_ai, "_resting", {})
    google_ai.mark_failed("google/gemini-a", "Error code: 429 - RESOURCE_EXHAUSTED")
    assert google_ai.models() == ["google/gemini-b"]
