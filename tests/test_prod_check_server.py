"""prod_check's server check judges a Cygnus by the provider it runs on.

It demanded 9router and three healthy models of every setup, so on a
student's PC - a Gemini key, no 9router - the routine check always failed
(2026-10-01). The author's setup keeps those demands.
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import prod_check  # noqa: E402

GEMINI = ["google/gemini-3.1-flash-lite", "google/gemini-3.6-flash"]


def test_the_authors_setup_still_needs_9router_and_three_models():
    st = {"provider": "legacy", "router_up": True, "router_models": ["ag/a"], "google_models": GEMINI[:1]}
    assert prod_check.server_problems(st, lambda: True) == ["only 2 healthy cloud models (ag/a, google/gemini-3.1-flash-lite)"]
    st.update(router_up=False, router_models=["ag/a", "ag/b", "ag/c"])
    assert prod_check.server_problems(st, lambda: True) == ["9router is not running"]
    st.update(router_up=True)
    assert prod_check.server_problems(st, lambda: False) == ["9router's port is open but it does not answer"]
    assert prod_check.server_problems(st, lambda: True) == []


def test_a_students_gemini_key_needs_no_router():
    st = {"provider": "gemini", "router_up": False, "router_models": [], "google_models": GEMINI}
    assert prod_check.server_problems(st, lambda: False) == []


def test_gemini_with_every_model_resting_fails():
    st = {"provider": "gemini", "router_up": False, "router_models": [], "google_models": []}
    assert "no Gemini model is answering" in prod_check.server_problems(st)[0]


def test_a_setup_problem_is_the_answer():
    st = {"provider": "gemini", "setup_problem": "No API key is set for gemini yet.", "google_models": []}
    assert prod_check.server_problems(st) == ["No API key is set for gemini yet."]


def test_an_own_endpoint_that_does_not_answer_is_named():
    st = {"provider": "openai", "router_up": False, "endpoint": "https://api.openai.com/v1",
          "router_models": [], "google_models": []}
    assert prod_check.server_problems(st) == ["the openai endpoint does not answer (https://api.openai.com/v1)"]
    st["router_up"] = True
    assert prod_check.server_problems(st) == []


def test_on_ollama_the_local_model_answering_is_the_point():
    st = {"provider": "ollama", "router_up": False, "router_models": [], "google_models": [],
          "last_backend": "ollama"}
    assert prod_check.server_problems(st) == []
    st["provider"] = "groq"
    st["router_up"] = True
    assert prod_check.server_problems(st) == ["the last turn was answered by the local model"]


def test_every_check_main_runs_is_there():
    """Rewriting check_server once dropped _run, check_compile, check_tests
    and check_routing with it; importing still worked, only main broke."""
    for name in ("_run", "check_compile", "check_tests", "check_routing", "check_server", "check_smoke"):
        assert callable(getattr(prod_check, name, None)), name
