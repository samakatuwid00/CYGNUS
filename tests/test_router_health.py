"""router_health: the health table the brain's 9router chain reads, and the
words for which model is answering. No network: probes are not run here."""

import time

import pytest

import router_health as rh


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(rh, "_state", {})
    monkeypatch.setattr(rh, "_last_switch_notice", 0.0)
    monkeypatch.setattr(rh, "candidates", lambda: ["ag/gemini-3.8-flash-low", "ag/claude-sonnet-4-6",
                                                   "gc/gemini-2.5-flash", "cu/claude-4.5-sonnet"])


@pytest.mark.parametrize("model, name", [
    ("ag/gemini-3.8-flash-low", "Gemini 3.8 Flash"),
    ("ag/claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("cu/claude-4.6-sonnet-medium-thinking", "Claude 4.6 Sonnet"),
    ("gh/gpt-5.4-mini", "GPT-5.4 Mini"),
    ("cl/anthropic/claude-sonnet-4.6", "Claude Sonnet 4.6"),
    ("jarvis-qwen3", "the local Qwen model"),
    ("oc/big-pickle", "Big Pickle"),
    ("oc/mimo-v2.5-free", "MiMo v2.5"),
    ("oc/deepseek-v4-flash-free", "DeepSeek v4 Flash"),
])
def test_models_have_names_people_know(model, name):
    assert rh.friendly(model) == name


def test_nothing_is_healthy_before_the_first_probe():
    assert rh.healthy_models() == []


def test_healthy_models_keep_the_owner_order_and_skip_cooling_ones():
    rh.mark_healthy("cu/claude-4.5-sonnet", 0.6)
    rh.mark_healthy("gc/gemini-2.5-flash", 2.1)
    rh.mark_healthy("ag/claude-sonnet-4-6", 1.0)
    rh.mark_unhealthy("ag/claude-sonnet-4-6", "Unavailable (reset after 130h 29m 34s)")
    assert rh.healthy_models() == ["gc/gemini-2.5-flash", "cu/claude-4.5-sonnet"]


@pytest.mark.parametrize("reason, seconds", [
    ("[antigravity/gemini-3.8-flash-low] Unavailable (reset after 95h 47m 7s)", 95 * 3600 + 47 * 60 + 7),
    ("[gemini-cli/gemini-2.5-flash] [403]: HTTP 403 (reset after 2m)", 120),
    ("empty reply", rh.EMPTY_COOLDOWN_S),
    ("Error code: 429", rh.DEFAULT_COOLDOWN_S),
])
def test_a_failure_waits_until_the_provider_says(reason, seconds):
    before = time.time()
    rh.mark_unhealthy("gc/gemini-2.5-flash", reason)
    until = rh.snapshot()["gc/gemini-2.5-flash"]["until"]
    assert abs((until - before) - seconds) < 2


def test_a_restart_knows_what_answered(tmp_path, monkeypatch):
    path = str(tmp_path / "router_health.json")
    rh.mark_healthy("cu/claude-4.5-sonnet", 0.6)
    rh.mark_unhealthy("ag/claude-sonnet-4-6", "Unavailable (reset after 95h 47m 7s)")
    rh.save(path)
    monkeypatch.setattr(rh, "_state", {})                    # a fresh process
    assert rh.load(path) is True
    assert rh.healthy_models() == ["cu/claude-4.5-sonnet"]
    assert "ag/claude-sonnet-4-6" not in rh.healthy_models()


def test_an_old_saved_table_is_not_trusted_for_health(tmp_path, monkeypatch):
    path = str(tmp_path / "router_health.json")
    rh.mark_healthy("cu/claude-4.5-sonnet", 0.6)
    rh.mark_unhealthy("ag/claude-sonnet-4-6", "Unavailable (reset after 95h 47m 7s)")
    rh.save(path)
    monkeypatch.setattr(rh, "_state", {})
    rh.load(path, now=time.time() + rh.CACHE_FRESH_S + 5)    # saved too long ago
    assert rh.healthy_models() == []                         # health must be re-checked
    assert rh.snapshot()["ag/claude-sonnet-4-6"]["ok"] is False   # a named reset still holds


def test_a_switch_is_announced_once_per_turn():
    first = rh.switch_notice("ag/gemini-3.8-flash-low", "ag/claude-sonnet-4-6")
    assert first == "Gemini 3.8 Flash isn't available right now, so I'm switching to another model, sir."
    assert rh.switch_notice("ag/claude-sonnet-4-6", "gc/gemini-2.5-flash") is None


def test_the_answering_model_is_named_when_it_changes(monkeypatch):
    monkeypatch.setattr(rh, "primary", lambda: "Gemini 3.8 Flash")
    assert rh.answer_notice("oc/big-pickle", "router", "Gemini 3.8 Flash") == \
        "Big Pickle, OpenCode's free model, is answering while Gemini 3.8 Flash is unavailable."
    assert rh.answer_notice("cu/claude-4.5-sonnet", "router", "Gemini 3.8 Flash") == \
        "Claude 4.5 Sonnet through Cursor is answering while Gemini 3.8 Flash is unavailable."
    assert rh.answer_notice("ag/gemini-3.8-flash-low", "router", "Big Pickle") == \
        "Gemini 3.8 Flash is back and answering again."
    assert rh.answer_notice("ag/gemini-3.8-flash-low", "router", "Gemini 3.8 Flash") is None
    assert "local model is answering" in rh.answer_notice(None, "ollama", "Gemini 3.8 Flash")
    assert rh.answer_notice(None, "ollama", "the local model") is None
    assert rh.answer_notice(None, "tool", "Gemini 3.8 Flash") is None


def test_a_reconnecting_client_is_not_told_again(monkeypatch):
    monkeypatch.setattr(rh, "primary", lambda: "Gemini 3.8 Flash")
    monkeypatch.setattr(rh, "_heard", {})
    assert rh.notice_for("remote", "oc/big-pickle", "router")          # first time: said
    assert rh.notice_for("remote", "oc/big-pickle", "router") is None  # same phone, new socket
    assert rh.notice_for("desktop", "oc/big-pickle", "router")         # another device: said once
    assert rh.notice_for("remote", None, "instant") is None            # a tool turn changes nothing
    assert rh.notice_for("remote", "oc/big-pickle", "router") is None


# ---- 2026-09-22: tool-call probe, dead providers, router down, ranking ----

@pytest.mark.parametrize("reason", [
    "[kilocode/deepseek/deepseek-chat] [402]: Paid Model - Credits Required (reset after 1m 22s)",
    "[github/gpt-5.4] [403]: unauthorized: not licensed to use Copilot",
    "FreeTierError: OpenCode's free tier can only be used from within OpenCode",
    "[qoder/qmodel_38max] [403]: pricingUrl https://qoder.com/pricing (reset after 2m)"])
def test_a_provider_that_will_not_answer_waits_a_day(reason):
    before = time.time()
    rh.mark_unhealthy("kc/x", reason)
    assert rh.snapshot()["kc/x"]["until"] - before >= rh.DEAD_COOLDOWN_S - 2


def test_a_router_that_is_down_is_not_blamed_on_its_models():
    rh.mark_healthy("ag/claude-sonnet-4-6", 1.0)
    rh.mark_unhealthy("ag/claude-sonnet-4-6", "APIConnectionError: Connection error.")
    assert rh.healthy_models() == ["ag/claude-sonnet-4-6"]


def test_the_owners_models_lead_then_the_fastest(monkeypatch):
    monkeypatch.undo()                                   # the real candidates()
    monkeypatch.setattr(rh, "_state", {})
    monkeypatch.setattr(rh, "configured", lambda: ["ag/claude-sonnet-4-6", "ag/claude-opus-4-6-thinking"])
    monkeypatch.setattr(rh, "EXTRA_CANDIDATES", ["ag/gemini-3.8-flash-low", "qd/qfmodel", "cx/gpt-5.5"])
    monkeypatch.setattr(rh, "_discovered", ["ag/new-model"])
    rh.mark_healthy("ag/gemini-3.8-flash-low", 11.3)
    rh.mark_healthy("qd/qfmodel", 3.5)
    rh.mark_unhealthy("cx/gpt-5.5", "[429]: The usage limit has been reached (reset after 7m 32s)")
    assert rh.candidates() == ["ag/claude-sonnet-4-6", "ag/claude-opus-4-6-thinking",
                               "qd/qfmodel", "ag/gemini-3.8-flash-low", "ag/new-model", "cx/gpt-5.5"]


class _Client:
    def __init__(self, replies):
        from types import SimpleNamespace as NS
        self.replies = replies
        self.chat = NS(completions=NS(create=self.create))

    def create(self, model, **kw):
        from types import SimpleNamespace as NS
        kind = self.replies[model]
        if isinstance(kind, Exception):
            raise kind
        call = [NS(function=NS(name="open_application", arguments='{"app": "notepad"}'))]
        msg = NS(content="" if kind == "tool" else kind, tool_calls=call if kind == "tool" else None)
        return NS(choices=[NS(message=msg)])


def test_the_probe_counts_only_a_tool_call(monkeypatch, tmp_path):
    import openai
    import router_keeper
    replies = {"ag/claude-sonnet-4-6": "tool",
               "ag/gemini-3.5-flash-low": "Gemini 3.5 Flash is no longer available.",
               "cu/claude-4.5-sonnet": ""}
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: _Client(replies))
    monkeypatch.setattr(router_keeper, "port_open", lambda timeout=0.5: True)
    monkeypatch.setattr(rh, "discover", lambda force=False: [])
    monkeypatch.setattr(rh, "CACHE_PATH", str(tmp_path / "rh.json"))
    rh.probe_once(list(replies))
    snap = rh.snapshot()
    assert snap["ag/claude-sonnet-4-6"]["ok"] is True
    assert snap["ag/gemini-3.5-flash-low"]["ok"] is False and "no tool call" in snap["ag/gemini-3.5-flash-low"]["reason"]
    assert snap["cu/claude-4.5-sonnet"]["ok"] is False


def test_the_probe_starts_a_router_that_is_down_and_marks_nothing(monkeypatch):
    import router_keeper
    started = []
    monkeypatch.setattr(router_keeper, "port_open", lambda timeout=0.5: False)
    monkeypatch.setattr(router_keeper, "ensure_up", lambda wait=20: started.append(wait) or False)
    rh.mark_healthy("ag/claude-sonnet-4-6", 1.0)
    rh.probe_once()
    assert started == [0]
    assert rh.healthy_models() == ["ag/claude-sonnet-4-6"]


def test_the_local_model_notice_names_a_router_that_is_down(monkeypatch):
    import router_keeper
    monkeypatch.setattr(router_keeper, "state", {"up": False})
    assert rh.answer_notice(None, "ollama", "Claude Sonnet 4.6").startswith("9router isn't running")
