"""router_health.py - which 9router models can answer right now.

9router fronts many providers, and on 2026-09-12 most were down at once:
antigravity out of quota for four days (503 "Unavailable (reset after 95h
47m)"), github / cline / kilocode unlicensed or paid, gemini-cli 403 for a
few minutes, and cursor answering 200 with no text at all. Cygnus tried the
same dead models on every turn and ended on the slow local model.

This keeps a small health table, filled by a background probe every few
minutes and by the brain's own failed calls (mark_unhealthy), so the chain
can ask healthy_models() on every turn without waiting on the network. A
model counts as healthy only when it returned real text: an empty 200 is a
failure. friendly() and the notice functions word "which model is answering"
for the user.

Since 2026-09-22 a model counts as healthy only when it calls a tool (Cygnus
cannot work without them, and ag/gemini-3.5-flash-low answered "Gemini 3.5
Flash is no longer available" as ordinary text); providers that say they will
never answer (paid credits, unlicensed, OpenCode-only) wait a day, not five
minutes; a router that is down is not blamed on its models; and models 9router
offers that nobody listed are found from its /v1/models and tried too.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from cygnus import paths

PROBE_EVERY_S = float(os.getenv("JARVIS_ROUTER_PROBE_S", "300"))
PROBE_TIMEOUT_S = 20.0
DEFAULT_COOLDOWN_S = 300.0
EMPTY_COOLDOWN_S = 1800.0          # a provider that answers with nothing is broken, not busy
DEAD_COOLDOWN_S = 86400.0          # paid, unlicensed or OpenCode-only: not coming back soon
# A healthy model is asked again only this often; live calls keep it fresh.
HEALTHY_RECHECK_S = float(os.getenv("JARVIS_ROUTER_RECHECK_S", "900"))
DISCOVER_EVERY_S = 6 * 3600.0
# The provider's own words for "this will not answer until someone pays or
# signs in" (2026-09-22 probe: kilocode and cline 402, github "not licensed",
# qoder's pricing page, OpenCode's free tier outside OpenCode). 9router adds a
# short "reset after" to these too; it is not believed.
_DEAD_RE = re.compile(r"\[402\]|\b402\b|not licensed|Credits Required|insufficient_credits|"
                      r"FreeTierError|can only be used from within|pricingUrl", re.I)
# Tried after the configured ROUTER_MODEL + ROUTER_FALLBACK_MODELS, fastest
# measured first (candidates() ranks them). The list below is the 2026-09-22
# probe with a real tool call: every ag/ model here and qd/qfmodel called the
# tool in 2-12 s; codex was out of quota for minutes and gemini-cli 403 for a
# minute, both kept for when they reset. OpenCode's free tier ("can only be
# used from within OpenCode"), github (unlicensed), cline and kilocode (paid)
# and cursor (empty replies) are gone from it. History: measured 2026-09-12 with Cygnus's real prompt and all 32
# tools (7.5k tokens), big-pickle answered in 2.6-6 s and called tools right,
# ling-3.0-flash in 3-5 s, mimo-v2.5 and nemotron-3.5-lightning in 7-8 s.
# Left out: nemotron-3-ultra (92 s), deepseek-v4-flash-free (400 errors),
# muse-spark (empty replies). The codex models are a provider of their own:
# on 2026-09-14 antigravity was out of quota again, every configured model
# with it, and cx/gpt-5.6-luna and cx/gpt-5.5 answered in about 2 s, but the
# probe never tried them.
EXTRA_CANDIDATES = [m.strip() for m in os.getenv(
    "JARVIS_ROUTER_CANDIDATES",
    "ag/claude-sonnet-4-6,ag/claude-opus-4-6-thinking,ag/gemini-pro-agent,ag/gemini-3.1-pro-low,"
    "ag/gemini-3-flash,qd/qfmodel,ag/gemini-3.7-flash-medium,ag/gemini-3.6-flash-low,"
    "ag/gemini-3.8-flash,ag/gemini-3.8-flash-low,"
    "cx/gpt-5.6-luna,cx/gpt-5.5,cx/gpt-5.4-mini,gc/gemini-2.5-flash,gc/gemini-3-flash-preview"
).split(",") if m.strip()]
# Found in 9router's /v1/models and in neither list; probed like the rest.
_discovered: list[str] = []
_last_discover = 0.0
_SKIP_DISCOVER_RE = re.compile(r"-review$|opus-max|high-thinking", re.I)

_lock = threading.Lock()
_state: dict[str, dict] = {}       # model -> {"ok", "until", "latency", "reason", "ts"}
_thread = None
_last_switch_notice = 0.0
# The table survives a restart: a turn in the ~20 s before the first probe
# walked the dead models and ran out of time (2026-09-12, "Cygnus?" 47 s).
CACHE_PATH = paths.log("router_health.json")
CACHE_FRESH_S = 600.0


def configured() -> list[str]:
    try:
        from cygnus.config import ROUTER_MODEL, ROUTER_FALLBACK_MODELS
        return [m for m in [ROUTER_MODEL] + list(ROUTER_FALLBACK_MODELS) if m]
    except Exception:
        return []


def candidates() -> list[str]:
    """Every model worth trying: the owner's configured models in their order,
    then the rest fastest measured first (untested ones after the measured)."""
    own = list(dict.fromkeys(configured()))
    rest = [m for m in dict.fromkeys(EXTRA_CANDIDATES + _discovered) if m not in own]
    with _lock:
        def rank(m):
            st = _state.get(m) or {}
            if st.get("ok") and st.get("latency") is not None:
                return (0, st["latency"])
            return (1, 0.0) if not st else (2, 0.0)
        rest.sort(key=rank)
    return own + rest


def discover(force: bool = False) -> list[str]:
    """Add the models 9router offers that no list names (every six hours)."""
    global _last_discover
    if not force and time.time() - _last_discover < DISCOVER_EVERY_S:
        return list(_discovered)
    _last_discover = time.time()
    try:
        import httpx
        from cygnus.config import ROUTER_BASE_URL, ROUTER_API_KEY
        r = httpx.get(ROUTER_BASE_URL.rstrip("/") + "/models", timeout=10,
                      headers={"Authorization": f"Bearer {ROUTER_API_KEY or 'x'}"})
        ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
    except Exception:
        return list(_discovered)
    known = set(configured()) | set(EXTRA_CANDIDATES)
    for m in ids:
        if m not in known and m not in _discovered and not _SKIP_DISCOVER_RE.search(m):
            _discovered.append(m)
    return list(_discovered)


def healthy_models(now: float | None = None) -> list[str]:
    """Models that answered with real text at their last check and are not
    cooling down, best first. Instant: no network. [] until the first probe."""
    _ensure_probing()
    now = now or time.time()
    order = candidates()                # takes _lock itself: not while holding it
    with _lock:
        return [m for m in order
                if _state.get(m, {}).get("ok") and now >= _state[m].get("until", 0)]


def _cooldown(reason: str) -> float:
    if _DEAD_RE.search(reason or ""):
        return DEAD_COOLDOWN_S
    m = re.search(r"reset after\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", reason or "", re.I)
    if m and any(m.groups()):
        h, mi, s = (int(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    return EMPTY_COOLDOWN_S if "empty reply" in (reason or "") else DEFAULT_COOLDOWN_S


def mark_unhealthy(model: str, reason: str = "") -> None:
    """A live call to `model` failed (the brain calls this on 503 / 429 /
    errors / empty replies): skip it until its reset time, or five minutes.
    A call that never reached 9router says nothing about the model: the
    router is down (router_keeper restarts it), so the table is left alone."""
    try:
        from cygnus.brain import router_keeper
        if router_keeper.is_connection_error(reason):
            return
    except Exception:
        pass
    now = time.time()
    with _lock:
        _state[model] = {"ok": False, "until": now + _cooldown(reason),
                         "reason": " ".join(str(reason).split())[:160], "ts": now,
                         "latency": None}


def forgive_timeouts() -> int:
    """Drop the cooldowns a hung router caused ("Request timed out." on every
    model at once); router_keeper calls this after restarting it. Refusals the
    providers named (quota, 403, paid) stand. Returns how many were dropped."""
    with _lock:
        hit = [m for m, v in _state.items() if not v.get("ok") and "timed out" in v.get("reason", "").lower()]
        for m in hit:
            del _state[m]
    return len(hit)


def mark_healthy(model: str, latency: float | None = None) -> None:
    with _lock:
        _state[model] = {"ok": True, "until": 0, "reason": "", "ts": time.time(), "latency": latency}


_PROBE_TOOL = [{"type": "function", "function": {
    "name": "open_application", "description": "Open an installed application.",
    "parameters": {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}}}]
_PROBE_MESSAGES = [{"role": "system", "content": "You are a voice assistant with tools."},
                   {"role": "user", "content": "open notepad"}]


def _due(now: float) -> list[str]:
    """The models to ask this round: not cooling down, and not a healthy one
    checked within HEALTHY_RECHECK_S."""
    out = []
    order = candidates()
    with _lock:
        for m in order:
            st = _state.get(m) or {}
            if now < st.get("until", 0):
                continue
            if st.get("ok") and now - st.get("ts", 0) < HEALTHY_RECHECK_S:
                continue
            out.append(m)
    return out


def probe_once(models: list[str] | None = None) -> dict:
    """Ask each due model to call a tool, in parallel. A model that answers in
    words but calls no tool cannot run Cygnus: it counts as a failure."""
    import concurrent.futures as cf
    from openai import OpenAI
    from cygnus.config import ROUTER_BASE_URL, ROUTER_API_KEY
    try:
        from cygnus.brain import router_keeper
        if not router_keeper.port_open():
            # Every call would fail with "Connection error": that is the
            # router, not the models. Start it; probe next round.
            router_keeper.ensure_up(wait=0)
            return snapshot()
        if not router_keeper.responds(timeout=10):
            # Open but silent (hung): every model would time out and be
            # blamed for it. router_keeper restarts a router that stays so.
            return snapshot()
    except ImportError:
        pass
    discover()
    client = OpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY or "x",
                    max_retries=0, timeout=PROBE_TIMEOUT_S)
    todo = models or _due(time.time())

    def check(model):
        start = time.time()
        try:
            r = client.chat.completions.create(model=model, max_tokens=200, tools=_PROBE_TOOL,
                                               tool_choice="auto", messages=_PROBE_MESSAGES)
            msg = r.choices[0].message if r.choices else None
            if msg and msg.tool_calls:
                mark_healthy(model, round(time.time() - start, 2))
            elif msg and (msg.content or "").strip():
                mark_unhealthy(model, "empty reply: no tool call, said " + (msg.content or "")[:80])
            else:
                mark_unhealthy(model, "empty reply")
        except Exception as e:
            mark_unhealthy(model, str(e))

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(check, todo))
    save()
    return snapshot()


def snapshot() -> dict:
    with _lock:
        return {m: dict(v) for m, v in _state.items()}


def save(path: str | None = None) -> None:
    """Write the table so the next process starts knowing it."""
    import json
    path = path or CACHE_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"saved": time.time(), "state": snapshot()}, f)
    except Exception:
        pass


def load(path: str | None = None, now: float | None = None) -> bool:
    """Read a saved table if it is fresh. A healthy entry older than
    CACHE_FRESH_S is dropped (it may have died since); cooldowns are kept
    until they end, since the provider named the reset time."""
    import json
    path, now = path or CACHE_PATH, now or time.time()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    fresh = now - float(data.get("saved") or 0) < CACHE_FRESH_S
    kept = {m: v for m, v in (data.get("state") or {}).items()
            if (v.get("ok") and fresh) or (not v.get("ok") and now < v.get("until", 0))}
    with _lock:
        for m, v in kept.items():
            _state.setdefault(m, v)
    return bool(kept)


def _loop():
    while True:
        try:
            probe_once()
        except Exception as e:
            print(f"[ROUTER-HEALTH] probe failed: {e}", flush=True)
        time.sleep(PROBE_EVERY_S)


def start() -> None:
    """Begin probing now (the server calls this at boot) rather than on the
    first turn, which would otherwise still walk the dead models. The last
    saved table is read first, so a restart knows at once what answers."""
    load()
    _ensure_probing()


def _ensure_probing() -> None:
    """Start the background probe once per process (never under pytest)."""
    global _thread
    if _thread is not None or os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules \
            or os.getenv("JARVIS_ROUTER_PROBE", "1") == "0":
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="router-health")
    _thread.start()


# ------------------------------------------------------------------ wording

def friendly(model: str) -> str:
    """'ag/gemini-3.8-flash-low' -> 'Gemini 3.8 Flash', 'ag/claude-sonnet-4-6'
    -> 'Claude Sonnet 4.6', 'gh/gpt-5.4-mini' -> 'GPT-5.4 Mini'."""
    name = str(model or "").split("/")[-1].lower()
    if name.startswith("jarvis-qwen") or name.startswith("qwen"):
        return "the local Qwen model"
    name = re.sub(r"-(?:extra-low|low|medium|high|preview|agent|thinking|max|review|free|"
                  r"contributor)\b", "", name)
    name = re.sub(r"(\d)-(\d)(?=$|-)", r"\1.\2", name)
    words = [{"gpt": "GPT", "mimo": "MiMo", "glm": "GLM", "deepseek": "DeepSeek"}.get(p)
             or (p if p[:1].isdigit() or re.match(r"v\d", p) else p.capitalize())
             for p in name.split("-") if p]
    return re.sub(r"GPT (\d)", r"GPT-\1", " ".join(words)) or str(model)


def switch_notice(failed_model: str, next_model: str) -> str | None:
    """Said the moment the chain gives up on a model (brain_gemini calls it).
    Once per turn's worth of hops: a chain that walks past five dead models
    must not speak five times."""
    global _last_switch_notice
    now = time.time()
    if now - _last_switch_notice < 30:
        return None
    _last_switch_notice = now
    return f"{friendly(failed_model)} isn't available right now, so I'm switching to another model, sir."


def answering(model: str | None, backend: str | None) -> str | None:
    """Who answered a turn, in the user's words, or None for a local tool."""
    if backend == "ollama":
        return "the local model"
    if model:
        return friendly(model)
    return {"cerebras": "Cerebras", "groq": "Groq", "gemini": "Gemini"}.get(backend or "")


_PROVIDER = {"oc": "OpenCode's free model", "gc": "through Gemini CLI", "cu": "through Cursor",
             "gh": "through GitHub Copilot", "cl": "through Cline", "kc": "through Kilo Code"}


def answer_notice(model: str | None, backend: str | None, previous: str | None) -> str | None:
    """The line said with a reply when the model answering changed since the
    last one this user heard, else None. Says why when it is a stand-in:
    "Big Pickle, OpenCode's free model, is answering while Gemini 3.8 Flash is
    unavailable." A bare "Big Pickle is answering now." puzzled the owner."""
    who = answering(model, backend)
    if not who or who == previous:
        return None
    if backend == "ollama":
        try:
            from cygnus.brain import router_keeper
            if router_keeper.state.get("up") is False:
                return ("9router isn't running and I couldn't start it, so the local model is "
                        "answering; it's slower.")
        except Exception:
            pass
        return "The cloud models are unavailable, so the local model is answering; it's slower."
    usual = primary()
    if who == usual:
        return f"{who} is back and answering again."
    prefix = str(model or "").split("/")[0]
    via = _PROVIDER.get(prefix)
    who_via = f"{who}, {via}," if via and not via.startswith("through") else (f"{who} {via}" if via else who)
    return (f"{who_via} is answering while {usual} is unavailable." if usual
            else f"{who_via} is answering now.")


# Who each user last heard was answering, by client ("local", "remote",
# "desktop", "session:<id>"). Per socket, every phone reconnect announced the
# stand-in model again (2026-09-12).
_heard: dict[str, str] = {}


def notice_for(client: str | None, model: str | None, backend: str | None) -> str | None:
    """answer_notice for this client, remembered across its reconnects."""
    key = client or "local"
    who = answering(model, backend)
    line = answer_notice(model, backend, _heard.get(key, primary()))
    if who:
        _heard[key] = who
    return line


def primary() -> str | None:
    """The configured first model, as the user would name it."""
    try:
        from cygnus.config import ROUTER_MODEL
        return friendly(ROUTER_MODEL)
    except Exception:
        return None
