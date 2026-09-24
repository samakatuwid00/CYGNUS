# Jarvis Brain - multi-backend (9router/mimo primary, Gemini fallback, demo as last resort)
import json
import os
import re
import time
from cygnus import paths

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError("google-genai not installed. Run: pip install google-genai")


# Configuration
from cygnus.config import (GEMINI_API_KEY, GEMINI_MODEL, JARVIS_USE_9ROUTER, ROUTER_BASE_URL, ROUTER_MODEL, ROUTER_API_KEY, ROUTER_FALLBACK_MODELS, JARVIS_USE_GROQ, GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL, JARVIS_USE_CEREBRAS, CEREBRAS_API_KEY, CEREBRAS_BASE_URL, CEREBRAS_MODEL, JARVIS_USE_OLLAMA, JARVIS_PREFER_LOCAL, OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_API_KEY, OLLAMA_MAX_TOKENS, OLLAMA_TIMEOUT, OLLAMA_KEEP_WARM, OLLAMA_WARM_INTERVAL, JARVIS_LOCAL_ONLY, LOCAL_HISTORY_TOKEN_BUDGET, IDLE_RESET_MINUTES, ROUTER_HISTORY_TOKEN_BUDGET, TOOL_RESULT_HISTORY_CHARS)

MAX_HISTORY = 20

# Thread-local progress callback so tools can stream status updates back to the
# WebSocket handler while think() is still running. Set once at the top of
# think(), read by tools via _progress(). Costs nothing when unset (the common
# path for non-voice callers).
import threading as _threading
_progress_local = _threading.local()

def _progress(msg: str):
    """Emit a mid-task status update if a progress callback is set."""
    cb = getattr(_progress_local, "cb", None)
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _estimate_tokens(messages) -> int:
    """Rough token count for a built message list: ~4 characters per token.

    Deliberately an estimate and not a tokenizer call — it only has to decide
    whether history is small, and a real tokenizer would be another dependency
    and another per-turn cost.
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content) // 4
        elif content:
            total += len(str(content)) // 4
        for tc in (m.get("tool_calls") or []):
            total += len(str(tc)) // 4
        total += 4  # per-message role and framing overhead
    return total


# ---------------------------------------------------------------- fast path
# P1: answer trivial intents locally, with NO cloud call and NO tool schema.
# This is the single biggest latency win for "simple tasks": math, time/date,
# greetings and thanks are resolved instantly instead of paying the ~4s 9router
# round-trip (plus the ~2.7k-token tool prefix that P3 would otherwise still send).
import ast
import datetime

# Intents that can NEVER need a tool. Used by both the fast path (P1) and the
# tool-gating in _think_router (P3): a "simple" intent skips the tool schema.
SIMPLE_INTENTS = {"math", "greeting", "thanks", "time", "help",
                  "clarify_play", "clarify_search", "clarify_open"}

# Bare "open" names neither an app nor a site. Asked verbatim so the follow-up
# turn can recognise its own question (see resolve_open_choice / think()).
OPEN_CLARIFY_Q = "Open an app or a website, sir?"

def _safe_eval(expr: str):
    """Evaluate a basic arithmetic expression safely via AST (no builtins/names)."""
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
               ast.USub, ast.UAdd, ast.FloorDiv)
    node = ast.parse(expr, mode="eval")
    if not isinstance(node, ast.Expression):
        raise ValueError("not an expression")
    for n in ast.walk(node):
        if not isinstance(n, allowed):
            raise ValueError("disallowed syntax")
    return eval(compile(node, "<expr>", "eval"), {"__builtins__": {}}, {})

def _fmt_num(x):
    if isinstance(x, float):
        # Trim float noise: 4.0 -> "4", 3.3333333 -> "3.33"
        if x == int(x):
            return str(int(x))
        return f"{x:.2f}".rstrip("0").rstrip(".")
    return str(x)

# add_site: "add hackernews, news.ycombinator.com" is a registry WRITE, but every
# phrasing of it carries a URL-shaped token, so classify_intent used to hand it to
# the web_browse branch below — JARVIS fetched and read out the page and no entry
# was ever written. Both orders are accepted ("add <name>, <url>" and
# "add <url> as <name>"); a URL token is required, so "add milk to the list" is
# untouched.
_ADD_SITE_URL = r"(?:https?://\S+|(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?)"
_ADD_SITE_HEAD = (r"^(?:please\s+|(?:jarvis|cygnus)[,!]?\s+)*(?:add|register|bookmark)\s+"
                  r"(?:(?:a|the)\s+)?(?:new\s+)?(?:site|website|url|link)?\s*")
_ADD_SITE_RE = re.compile(
    _ADD_SITE_HEAD +
    r"(?P<name>[\w][\w '\-]*?)\s*(?:,|:|=|\bas\b|\bat\b|\bis\b|\s)\s*"
    r"(?P<url>" + _ADD_SITE_URL + r")\s*[.!?]*$", re.I)
_ADD_SITE_REV_RE = re.compile(
    _ADD_SITE_HEAD +
    r"(?P<url>" + _ADD_SITE_URL + r")\s*"
    r"(?:,|:|\bas\b|\bcalled\b|\bnamed\b)\s*"
    r"(?P<name>[\w][\w '\-]*?)\s*[.!?]*$", re.I)


def parse_add_site(text: str):
    """(name, url) for an 'add <name>, <url>' request, else None."""
    t = " ".join((text or "").strip().split())
    for rx in (_ADD_SITE_RE, _ADD_SITE_REV_RE):
        m = rx.match(t)
        if not m:
            continue
        name = m.group("name").strip(" ,.'-").lower()
        url = m.group("url").strip(" ,.")
        if name and url and not name.startswith("http"):
            return name, url
    return None


def _run_bounded(fn, budget, label):
    """Run `fn()` on a worker thread, giving up after `budget` seconds.

    Returns (result, timed_out). Used by routes whose tool can block for
    minutes on a browser it cannot reach: the turn must end in an honest
    sentence, never in silence. The worker is abandoned, not killed — it can
    still finish its own work, it just no longer owns the reply.
    """
    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"jv-{label}")
    try:
        return ex.submit(fn).result(timeout=budget), False
    except _cf.TimeoutError:
        return None, True
    finally:
        ex.shutdown(wait=False)


# One budget per turn for the whole model chain. A model that is down held a
# turn for minutes - 9router's per-model retries at 45 s each, then 180 s on
# the CPU model - and turns stacked up behind each other (2026-09-12).
# The chain's OpenAI clients are built with max_retries=0: the SDK's default
# retried a timed-out call twice, so one 40 s Ollama wait became two minutes.
TURN_BUDGET = float(os.getenv("JARVIS_TURN_BUDGET", "45"))
MIN_CALL_SECONDS = 2.0
# A model's first call in a turn: the healthy ones answer in 2-12 s (probe,
# 2026-09-22), so one that has not answered in 20 s gives the next model the
# rest of the turn. At 45 s a single hung Sonnet call used the whole turn and
# the answer came from the local model. Later calls in the tool loop keep 45 s.
FIRST_CALL_S = float(os.getenv("JARVIS_FIRST_CALL_S", "20"))
# Of the turn, what may go to waiting for a 9router that had to be started.
ROUTER_START_WAIT_S = 20.0
_NO_MODEL_REPLY = ("I couldn't get an answer from my AI model in time, sir. The online service "
                   "may be down - please try again in a moment.")


def _why_no_model(err) -> str:
    """The reason the models gave, as a person would say it. Every failure
    used to read the same, and a canned reply even answered as if it had
    opened an app while nothing ran (bench/chaos.py, 2026-09-23)."""
    text = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err or "")
    if not text:
        return "No model answered, sir."
    low = text.lower()
    try:
        from cygnus.brain import router_keeper
        down = router_keeper.is_connection_error(text)
    except Exception:
        down = "connection error" in low
    if down or "routerdown" in low or "is not running" in low:
        return ("I couldn't reach the model endpoint, sir - it isn't answering. "
                "Check that it is running and that ROUTER_BASE_URL is right.")
    if "429" in text or "quota" in low or "usage limit" in low or "rate" in low:
        m = re.search(r"reset after\s*([0-9hms \t]+)", text, re.I)
        when = f" It resets in {m.group(1).strip()}." if m else ""
        return f"My model is out of quota, sir.{when} I'll use another one as soon as it answers."
    if "timed out" in low or "timeout" in low or "budget" in low:
        return "My model didn't answer in time, sir. Please try again in a moment."
    if "empty" in low:
        return "My model answered with nothing, sir - that isn't an answer, so I didn't use it."
    return f"No model could answer that, sir: {text.split(chr(10))[0][:120]}"


class _OutOfTime(Exception):
    """The turn's budget is spent; no further model call starts."""


def _time_left():
    deadline = getattr(_progress_local, "deadline", None)
    return float("inf") if deadline is None else deadline - time.monotonic()


def _call_timeout(cap):
    """A model call's timeout: its own cap, or what the turn has left."""
    left = _time_left()
    if left < MIN_CALL_SECONDS:
        raise _OutOfTime(f"turn budget of {TURN_BUDGET:.0f}s spent")
    return min(cap, left)


def _router_health():
    """The live 9router health module, or None until it exists."""
    try:
        import importlib as _il
        router_health = _il.import_module("cygnus.brain.router_health")   # honours sys.modules (tests swap in a stand-in)
        return router_health
    except Exception:
        return None


def _router_models():
    """9router models to try, best first: the models router_health says are
    answering right now, else the configured primary and its fallbacks."""
    configured = [ROUTER_MODEL] + [m for m in ROUTER_FALLBACK_MODELS if m != ROUTER_MODEL]
    health = _router_health()
    try:
        healthy = [m for m in (health.healthy_models() if health else []) if m]
    except Exception:
        healthy = []
    return healthy or configured


class _RouterDown(Exception):
    """9router is not answering on its port and could not be started."""


def _router_ready():
    """Make sure 9router is up before a turn walks its models. When it is
    down every model fails with "Connection error" and the turn used to land
    on the local model; router_keeper starts it and this waits (bounded by
    the turn). Raises _RouterDown when it stays down."""
    try:
        from cygnus.brain import router_keeper
    except ImportError:
        return
    if router_keeper.port_open():
        return
    wait = max(0.0, min(ROUTER_START_WAIT_S, _time_left() - 8.0))
    print(f"[JARVIS] 9router is not running; starting it (waiting up to {wait:.0f}s)", flush=True)
    if not router_keeper.ensure_up(wait=wait):
        raise _RouterDown(router_keeper.state.get("last_error") or "9router is not running")


def _report_healthy(model, latency):
    health = _router_health()
    if health and hasattr(health, "mark_healthy"):
        try:
            health.mark_healthy(model, round(latency, 2))
        except Exception:
            pass


def _report_unhealthy(model, reason):
    """Tell router_health a model just failed a live call, if it listens."""
    health = _router_health()
    if health and hasattr(health, "mark_unhealthy"):
        try:
            health.mark_unhealthy(model, str(reason)[:200])
        except Exception:
            pass


def _announce_model_switch(failed, model):
    """Say, while the turn runs, that another model is taking over. The words
    are router_health's: its switch_notice() when it has one, else the
    "model_switch:<from>-><to>" signal its reader turns into words and a HUD
    line. Nothing before router_health exists, so the raw signal is never
    spoken."""
    health = _router_health()
    if not health:
        return
    try:
        notice = (health.switch_notice(failed, model) if hasattr(health, "switch_notice")
                  else f"model_switch:{failed}->{model}")
    except Exception:
        notice = None
    cb = getattr(_progress_local, "cb", None)
    if notice and callable(cb):
        cb(notice)


def _is_open_target(name):
    """Whether `name` is a registered app or site (the chain splitter's test
    for "open vscode, notepad and chrome")."""
    try:
        from cygnus.apps import capabilities as machine_capabilities
        from cygnus import tools
        kind, found = tools.resolve_open_target(name)
        # A referent ("it", "the other one") is not a link in an open chain:
        # the splitter would have made "open vscode and it" two openable names.
        if kind == "clarify" and isinstance(found, dict) and found.get("referent"):
            return False
        if kind in ("app", "site", "clarify"):
            return True
        # An alias open_application knows ("calculator" -> calc, "vscode" ->
        # code) or a registry name spelled another way counts too - never
        # _find_app_by_name, whose word-overlap scoring took "what is AI" for
        # an app called "what is new in the latest version".
        clean = tools._clean_app_name(name).lower().strip()
        apps = (machine_capabilities.load_registry() or {}).get("apps", {})
        return clean in tools.APP_ALIASES or clean in apps or \
            machine_capabilities.resolve_normalized(apps, clean) is not None
    except Exception:
        return False


def _changes_things(text):
    """A request that changes the machine (delete, write, install ...). It goes
    to the confirm gate whatever the router says, so the router's model call
    is skipped: it took 8-10 s and came back "invalid" for "delete the file
    ..." (2026-09-12)."""
    try:
        from cygnus import tools
        return bool(tools._MUTATING_RE.search(text or ""))
    except Exception:
        return False


def _lead_llm(prompt):
    """The intent router's model call on a reply's path: answering cloud
    models only. The dead 9router model, then the CPU model, held a delete
    request for 25 s before its confirm (2026-09-12); with no cloud model up
    the router simply does not lead, and routing carries on."""
    from cygnus.rules import ai as rules_ai
    return rules_ai._ask_llm(prompt, local=False)


# The client of the most recent turn, for threads that were not started by it.
# _progress_local is thread-local, so a tool running on a worker thread sees
# nothing, and a confirm latched there was recorded as "default" (2026-09-23:
# the owner was told their own question belonged to another device).
_LAST_CLIENT = "default"


def _client_key():
    """Whose conversation this thread's turn belongs to (think(client=...)).

    This thread first; failing that, the last turn's client, which is right for
    the worker threads a turn spawns and harmless elsewhere - safety.same_client
    treats "default" as "nobody knows", never as "a different device".
    """
    return getattr(_progress_local, "client", None) or _LAST_CLIENT or "default"
# Moved to intents.py (restructure phase 5); re-exported so
# `tools.<name>` keeps working for callers and tests.
from cygnus.brain.intents import (
    NO_RULE_GUESS_INTENTS,  # noqa: E402,F401
    _ACKNOWLEDGE_RE,
    _APP_WORDS,
    _asks_about_own_work,
    _chained_device_verbs,
    _CHAINED_RE,
    _COMMAND_LEAD_RE,
    _DID_YOU_LEAD_RE,
    _FAVORITES_RE,
    _is_bare_check_in,
    _is_blank,
    _is_chained_command,
    _is_multistep_goal,
    _is_referent,
    _is_transport_referent,
    _MULTISTEP_ARTIFACT_RE,
    _MULTISTEP_CLAUSE_SPLIT_RE,
    _MULTISTEP_CONJ_RE,
    _MULTISTEP_CONN_RE,
    _MULTISTEP_DEVICE_VERB_RE,
    _MULTISTEP_INTENTS,
    _MULTISTEP_VERB_RE,
    _MUSIC_TAIL_RE,
    _MUSIC_VERB_RE,
    _OPEN_SITE_RE,
    _outer,
    _OWN_WORK_RE,
    _PREF_RE,
    _referent_object,
    _relays_the_only_command,
    _REPORT_TAIL_RE,
    _router_may_lead,
    _scrub,
    _SITE_LIKE_RE,
    _SITE_WORDS,
    _strip_correction,
    _WH_QUESTION_RE,
    BLANK_ASK,
    CLARIFY_ASK,
    classify_intent,
    default_report_path,
    parse_music_query,
    parse_open_site,
    parse_report_topic,
    play_favorites,
    SPOTIFY_ROUTE_BUDGET,
)


def _check_in_reply(text):
    """"ok" is acknowledged; "test" or "jarvis?" is someone checking in."""
    if _ACKNOWLEDGE_RE.fullmatch((text or "").strip()):
        return "Alright, sir."
    return "I'm here, sir. What can I do for you?"


def _check_in_for(spoken, routed):
    """The instant reply to a lone "test", "ok" or "Cygnus?", judged on the
    routed text and on the words as spoken: the routing strip turns
    "Cygnus?" into "?", which is not a check-in, and the turn waited out the
    model budget (2026-09-12). None when it is not a check-in."""
    for text in (routed, spoken):
        if _is_bare_check_in(text):
            return _check_in_reply(text)
    return None


# "can you / do you / is there ..." ending in a question mark.
_YES_NO_RE = re.compile(r"^\s*(?:(?:jarvis|cygnus|so|and|hey)[,\s]+)*"
                        r"(?:can|could|would|will|do|does|did|is|are|have|has)\s+"
                        r"(?:you|it|there|i|we|this|that)\b.*\?\s*$", re.I)
# A short lead-in, then a wh-question: "In one word, what colour is the sky
# on a clear day?" went to Hermes with a confirm - no wh-word first, and
# "clear" reads as a change to the machine (2026-09-12).
_LED_QUESTION_RE = re.compile(r"^[^?,]{1,40},\s*(?:what|which|who|whose|why|how|when|where)\b.*\?\s*$",
                              re.I)
# What a "can you X?" asks for when it is a request, not a capability question.
_REQUEST_ROUTES = {"task", "multi", "app_action", "open_site", "open_app", "close_app", "music",
                   "media_control", "web_search", "read_page", "report", "launch_project"}


def _is_question_not_request(text):
    """True for a question to answer, not a job to start: any what / which /
    who / how question, and a yes-no question the semantic router does not
    read as a request ("can you visit websites?" asks; "can you create a
    website with opencode?" asks for one). Until the router is loaded, a
    yes-no question stays a request, as before. A turn whose only imperative
    sits inside quoted or third-party content asks for nothing on the machine
    either, whatever that content demands."""
    t = (text or "").strip()
    if _relays_the_only_command(t):
        return True
    if _WH_QUESTION_RE.match(t) or _LED_QUESTION_RE.match(t):
        return True
    if not _YES_NO_RE.match(t):
        return False
    try:
        from cygnus.brain import semantic_route
        route, score = semantic_route.pick(t)
    except Exception:
        return False
    return route is not None and not (route in _REQUEST_ROUTES and score >= semantic_route.LEAD_MIN_SCORE)


def _own_work_facts(question=""):
    """What JARVIS did, from the job log and the audit trail: the jobs and
    file writes matching what the question names, then the latest ones. The
    latest three alone missed a morning's website by evening, and Cygnus
    said it had never built it (2026-09-12)."""
    parts = []
    try:
        from cygnus.jobs import registry as jobs
        matched = jobs.find_work(question)
        if matched:
            parts.append("Jobs matching what the user asked about, best match first:\n"
                         + jobs.format_jobs(matched))
        work = jobs.recent_work(exclude={j["id"] for j in matched})
        if work:
            parts.append("Latest background jobs, newest first:\n" + work)
    except Exception:
        pass
    try:
        from cygnus.jobs import registry as jobs
        from cygnus import tools
        words = sorted(jobs.content_words(question))
        writes = (tools.recent_file_writes(match=words) if words else []) or tools.recent_file_writes()
        if writes:
            parts.append("Files JARVIS wrote itself (write_file), oldest first:\n"
                         + "\n".join(f"- {w}" for w in writes))
    except Exception:
        pass
    if not parts:
        return ""
    return ("\n\n[WHAT JARVIS DID RECENTLY - facts from the job log and audit trail. "
            "Answer from these; say plainly when they do not cover the question.]\n"
            + "\n".join(parts) + "\n[END]")


# "Can you open github for me?" is an open too; without the request lead it
# went to the model router (3-7 s) instead of the registry fast lane.
_OPEN_LEAD_RE = re.compile(
    r"^(?:(?:please|jarvis|cygnus|hey|ok(?:ay)?|now|(?:can|could|would)\s+you)[,!\s]+)*"
    r"(?:open|launch|go\s+to|goto|visit)\s+(.+)$", re.I)


def _fast_lane_opens(text):
    """True when "open X" names an app or site the registries resolve: the
    delegate fast lane opens it in ~0.3 s, so the router (3-7 s) stays out
    of the way. "open new tab on brave" resolves nothing and goes to the
    router, which knows Brave's abilities."""
    m = _OPEN_LEAD_RE.match((text or "").strip())
    if not m:
        return False
    try:
        from cygnus import tools
        stripped, bword = tools._split_browser(m.group(1).rstrip(".!?"))
        kind, _ = tools.resolve_open_target(stripped, browser=tools._browser_key(bword) if bword else None)
        return kind in ("site", "app", "clarify")
    except Exception:
        return False


def _open_target(text):
    """What an open/visit request names - also for "can you open X for me",
    which parse_open_site does not read."""
    t = (text or "").strip()
    m = _OPEN_LEAD_RE.match(t.rstrip(".!?"))
    return parse_open_site(t) or (m.group(1) if m else None)


# Semantic routes that lead where the keywords found nothing specific, and
# the brain route each takes. recall_memory earned trust too, but the cloud
# brain already answers a specific question ("what is my name?") from the
# profile; the instant recall would recite the whole memory instead.
_SEMANTIC_LEAD = {"job_status": "job_status", "open_site": "open_site",
                  "app_action": "app_reference", "media_control": "app_reference",
                  "task": "general", "clarify": "clarify"}


def _semantic_lead(text):
    """The brain route a confident, trusted semantic pick leads to, or None."""
    try:
        from cygnus.brain import semantic_route
        route = semantic_route.lead(text)
    except Exception as e:
        print(f"[semantic] lead failed: {type(e).__name__}: {e}", flush=True)
        return None
    if route == "open_site" and not _open_target(text):
        return None
    return _SEMANTIC_LEAD.get(route)


def _looks_like_preference(t):
    """A stated preference ("I prefer lo-fi"), not a command that mentions
    one: "play some of my favorite on spotify" was being saved as a fact."""
    t = t or ""
    return bool(_PREF_RE.search(t)) and len(t.split()) <= 40 and not _COMMAND_LEAD_RE.match(t)


# A request, a correction or a question is never a preference, however many
# "I want"s it holds: "What I mean is ... I want you to utilize yourself" and
# "I want you to access my Facebook account" were saved as facts about the
# owner (2026-09-13).
_REQUEST_RE = re.compile(
    r"\b(?:want|need|like) you to\b|\b(?:can|could|would|will) you\b|\bhelp me\b"
    r"|^\s*(?:i mean|what i mean|i meant|actually)\b", re.I)
# Said outright, it is saved without asking. "I want" and "I need" lead
# requests as often as tastes, so they never count on their own.
_PREF_LEAD_RE = re.compile(
    r"^\s*(?:(?:and|also|so|well|oh)[,\s]+)*"
    r"(?:i (?:prefer|like|love|hate|always|never|usually|don'?t like|do not like)"
    r"|i'?m into|i am into|my (?:favou?rite|preferred|default))\b", re.I)
_PREF_STRONG_RE = re.compile(
    r"\b(?:i (?:prefer|like|love|hate|always|never|usually)|i'?m into|i am into"
    r"|my (?:favou?rite|preferred|default))\b", re.I)


def preference_kind(t):
    """'save' for a preference said outright ("I prefer lo-fi"), 'ask' for one
    that only might be ("for coding I usually prefer lo-fi"), None for the
    rest: commands, requests, corrections and questions."""
    t = (t or "").strip()
    if not _looks_like_preference(t) or t.endswith("?") or _REQUEST_RE.search(t):
        return None
    if _PREF_LEAD_RE.match(t):
        return "save"
    return "ask" if _PREF_STRONG_RE.search(t) else None


def _asks_something(reply):
    """The reply already puts a question or a confirm to the owner."""
    r = (reply or "").strip()
    return r.endswith("?") or "[NEEDS_CONFIRM" in r or r.startswith("[NEEDS_PICK]") or r.startswith("⟳")


def remember_question(text):
    """Ask before saving a preference that only might be one. The question,
    or '' when another question is already open (one at a time)."""
    from cygnus.brain import dialogue_state
    if dialogue_state.pending():
        return ""
    words = text.split()
    short = " ".join(words[:14]) + ("…" if len(words) > 14 else "")
    question = f"Should I remember that “{short.rstrip('.')}”?"
    dialogue_state.ask("remember", question, {"text": text}, owner="brain")
    return question


def answer_remember(text):
    """Yes or no to "Should I remember that …?", else None: anything longer
    is a new turn and routes as one."""
    from cygnus.brain import dialogue_state
    asked = dialogue_state.pending("remember")
    if not asked:
        return None
    from cygnus import tools
    reply = _consent.read(text, soft=True)
    if reply.kind == "yes":
        dialogue_state.answered()
        return tools.remember_fact(asked["payload"]["text"])
    if reply.kind == "no":
        dialogue_state.answered()
        return "Okay, I won't remember that."
    return None


def answer_running_stop(text):
    """Yes or no to "I'll stop ... Say confirm", else None. Read strictly: a
    bare "ok" does not stop anything."""
    from cygnus.brain import dialogue_state
    asked = dialogue_state.pending("running_stop")
    if not asked:
        return None
    reply = _consent.read(text)
    if reply.kind == "yes":
        dialogue_state.answered()
        from cygnus.apps import running as running_apps
        return running_apps.stop_now(asked["payload"])
    if reply.kind == "no":
        dialogue_state.answered()
        return "Okay, sir. I'll leave it running."
    return None


def answer_choice(text):
    """The reply to "Which one should I open?" or to a clarify question:
    (None, request) routes the chosen request as if it had been said
    outright, (reply, None) answers now, (None, None) when no such question
    is open. A reply that is not an answer drops the question: the owner
    moved on, and it is routed as the new request it is."""
    from cygnus.brain import dialogue_state
    asked = dialogue_state.pending("pick") or dialogue_state.pending("clarify")
    if not asked:
        return None, None
    payload = asked["payload"]
    options = payload.get("options") or []
    reply = _consent.read(text, options=payload.get("labels") or options)
    dialogue_state.answered()
    if reply.kind == "pick":
        chosen = options[reply.index]
        return None, (f"{payload['verb']} {chosen}" if payload.get("verb") else chosen)
    if reply.kind == "no":
        return "Okay, sir. I'll leave it.", None
    return None, None


def fast_path_answer(text: str):
    """Answer a trivial intent locally. Returns (answer, intent) or (None, intent)
    when the intent is not handled by the fast path."""
    t = (text or "").strip()
    intent = classify_intent(t)
    if intent == "math":
        expr = t.rstrip("= ").strip()
        try:
            val = _safe_eval(expr)
            return f"That's {_fmt_num(val)}, sir.", intent
        except Exception:
            return None, intent
    if intent == "greeting":
        return "Hello, sir. How may I assist you?", intent
    if intent == "thanks":
        return "You're welcome, sir.", intent
    if intent == "time":
        now = datetime.datetime.now()
        # Speak the time the way a person would.
        return (f"It is {now.strftime('%I:%M %p')} on "
                f"{now.strftime('%A, %B %d')}, sir."), intent
    if intent == "help":
        return ("I can answer questions, do quick math, tell you the time, "
                "search the web, write reports, and more, sir."), intent
    if intent == "clarify_play":
        return "Which song, sir? Name a track or an artist.", intent
    if intent == "clarify_search":
        return "What should I search for, sir?", intent
    if intent == CLARIFY_ASK:
        # A blank turn shares clarify_open's label (see CLARIFY_ASK) but not its
        # question: "Open an app or a website?" invents an open nobody asked for.
        return (BLANK_ASK if _is_blank(t) else OPEN_CLARIFY_Q), intent
    return None, intent


# Answer to OPEN_CLARIFY_Q: "an app" / "a website" / "a website, youtube".
_OPEN_CHOICE_RE = re.compile(
    r"^(?:an?\s+|the\s+)?(app|application|program|software|website|web\s?site|"
    r"site|web|url|page)\b[\s,:.\-]*(.*)$", re.I)


def last_assistant_message(conversation):
    """The most recent assistant turn, or '' — the user turn is already appended."""
    for m in reversed(conversation or []):
        if m.get("role") == "assistant":
            return (m.get("content") or "").strip()
    return ""


def resolve_open_choice(text: str):
    """Route the reply to OPEN_CLARIFY_Q.

    Returns (answer, rewritten_input). A bare choice gets the second half of the
    question; a choice that already names the target ("a website, youtube") is
    rewritten to "open youtube" so it routes through the normal open path
    instead of being answered as chat. (None, None) when the reply is neither —
    the turn then falls through to ordinary routing.
    """
    m = _OPEN_CHOICE_RE.match((text or "").strip())
    if not m:
        return None, None
    kind, rest = m.group(1).lower(), m.group(2).strip(" ,.?!")
    if rest:
        return None, f"open {rest}"
    if kind in ("app", "application", "program", "software"):
        return "Which app should I open, sir?", None
    return "Which website, sir? Name it or give me the address.", None


def _trim_history(messages, budget=LOCAL_HISTORY_TOKEN_BUDGET):
    """Drop the oldest turns until the replayed history fits `budget` tokens.

    messages[0] is the system prompt and is never dropped, and neither is the
    newest user message — that is the request being answered. Cuts land only on
    a user message so a tool result can never be left without the assistant
    tool_calls block it replies to; an orphaned tool message is a protocol error
    that the OpenAI-compatible backends reject outright.
    """
    if len(messages) <= 2:
        return messages
    head, rest = messages[0], messages[1:]
    last_user = 0
    for i, m in enumerate(rest):
        if m.get("role") == "user":
            last_user = i

    cut = 0
    while cut < last_user and _estimate_tokens(rest[cut:]) > budget:
        cut += 1
        while cut < last_user and rest[cut].get("role") != "user":
            cut += 1
    if cut == 0:
        return messages
    print(f"[JARVIS] history trimmed: dropped {cut} of {len(rest)} message(s) "
          f"to stay inside the local context window", flush=True)
    return [head] + rest[cut:]


def _truncate_for_history(result, limit=TOOL_RESULT_HISTORY_CHARS):
    """Clip a tool result down to what is worth REPLAYING on later turns.

    The current turn always receives the untruncated result — this only shapes
    the copy stored in self.conversation. Without it, one page read stays in
    every subsequent prompt for the life of the conversation.
    """
    text = result if isinstance(result, str) else str(result)
    if len(text) <= limit:
        return text
    marker = ("\n[... " + str(len(text) - limit) +
              " more characters truncated from replay history]")
    return text[:limit] + marker


# Moved to prompts.py (restructure phase 5); re-exported so
# `tools.<name>` keeps working for callers and tests.
from cygnus.brain.prompts import (  # noqa: E402,F401
    _ASTERISK_BLOCK,
    _clean_for_speech,
    _format_recall_for_speech,
    _load_jarvis_profile,
    _PROFILE_FILE,
    _profile_text,
    JARVIS_SYSTEM,
)

# Moved to tool_schema.py (restructure phase 5); re-exported so
# `tools.<name>` keeps working for callers and tests.
from cygnus.brain.tool_schema import (  # noqa: E402,F401
    _make_tool,
    _openai_tools,
    TOOL_DECLARATIONS,
    TOOLS,
)


# Flags that cause irreversible or outbound action. The model is not allowed to
# grant these to itself: mimo sets overwrite=true on a plain "write a note to
# X.txt", so consent has to come from the user's own words, which the model
# cannot fabricate.
_CONSENT_FLAGS = {
    "write_file": ("overwrite", re.compile(
        r"(?i)\b(overwrite|replace|clobber|update it|rewrite|wipe|yes|go ahead|do it|confirm)\b")),
    "ask_chatgpt": ("submit", re.compile(
        r"(?i)\b(send|submit|post|ask chatgpt|fire it|go ahead|do it|yes)\b")),
    # Same gate for the multi-AI writer: sending to Gemini/Claude/Copilot
    # requires the user's own words to ask for it.
    "ask_ai": ("submit", re.compile(
        r"(?i)\b(send|submit|post|fire it|go ahead|do it|yes|send it|then send|"
        r"and send)\b")),
}

# Tools whose `confirm` flag authorizes acting ON the machine. Each already has
# its own NEEDS_CONFIRM latch, but that latch lives inside the tool — this
# applies the same rule at the boundary: consent must appear in the USER's own
# words, which the model cannot fabricate. Defense in depth, one regex per call.
_CONFIRM_WORDS = re.compile(
    r"(?i)\b(confirm(?:ed)?|proceed|go ahead|do it|yes|approved?|"
    r"run it|execute it|send it|that'?s right|correct)\b")
for _consent_tool in ("desktop_control", "run_opencli", "delegate_to_hermes",
                      "delegate_to_hermes_grounded", "delegate", "run_autonomous"):
    _CONSENT_FLAGS[_consent_tool] = ("confirm", _CONFIRM_WORDS)

# A bare confirm utterance: ONLY the confirm words, nothing else. Must never
# match sentences that merely CONTAIN "yes"/"confirm" — those route normally.
# A bare yes or no to the confirm just asked, read the way every other
# question's answer is (consent.py): "yeah" and "sige" confirm, "ok" does not.
from cygnus.brain import consent as _consent
_BARE_CONFIRM_RE = _consent.YES_RE
_BARE_DECLINE_RE = _consent.NO_RE
# A model reply that reports an action. Only true when a tool ran this turn.
_ACTION_CLAIM_RE = re.compile(
    r"^\s*(?:(?:okay|ok|done|alright|sure)[,.!]?\s+)?(?:sir[,.]?\s+)?(?:i(?:'ve|\s+have)\s+)?"
    r"(?:opened|launched|closed|started|playing|now\s+playing|searching|searched|created|"
    r"deleted|removed|sent|moved|saved|turned|paused|stopped|muted)\b", re.I)
_MODEL_BACKENDS = {"router", "cerebras", "groq", "ollama", "gemini"}


_QUESTION_RE = re.compile(
    r"^\s*(?:(?:please|jarvis|cygnus|hey|ok(?:ay)?|so|now)[,\s]+)*"
    r"(?:what|which|who|how|why|when|where|did|do|does|have|has|is|are|can|could|tell me)\b", re.I)


def honest_reply(reply, ran, backend, user_text=""):
    """A model that answers "Opened notes app." when no tool ran this turn
    (`ran`: this turn's audit entries) told the user something false. Say
    what actually happened instead. A question ("how many things have you
    opened?") is answered, not acted on: its reply is never a claim."""
    asked = bool(_QUESTION_RE.match(user_text or "")) or (user_text or "").rstrip().endswith("?")
    if backend in _MODEL_BACKENDS and not ran and not asked and isinstance(reply, str) \
            and _ACTION_CLAIM_RE.match(reply):
        return ("I didn't actually do that, sir: nothing ran. Tell me exactly which "
                "app or site, like “open Obsidian”.")
    return reply


def _apply_consent_policy(name: str, args: dict, last_user_text: str) -> dict:
    """Downgrade a consent flag unless the user's own last message asked for it."""
    rule = _CONSENT_FLAGS.get(name)
    if not rule:
        return args
    flag, pattern = rule
    if not args.get(flag):
        return args
    if pattern.search(last_user_text or ""):
        return args
    args = dict(args)
    args[flag] = False
    print(f"[POLICY] {name}.{flag} downgraded to false - the user's words did not "
          f"ask for it: {last_user_text[:70]!r}", flush=True)
    return args


# A question about what already happened ("what happened?", "explain it",
# "status?") is answered, never acted on. On 2026-09-12 "What happened with
# that? Explain it simply." typed a prompt into Claude's site and tried to
# rewrite the page a job had just made.
_REPORT_ASK_RE = re.compile(
    r"^\s*(?:(?:please|cygnus|jarvis|hey|ok(?:ay)?|so|and|sir)[,\s]+)*"
    r"(?:what(?:'s|\s+is)?\s+(?:happened|the\s+status|going\s+on|did\s+you\s+(?:do|change|make))"
    r"|what\s+went\s+wrong|why\s+did|how\s+did\s+(?:it|that)\s+go|status\b"
    r"|explain|summari[sz]e|recap|tell\s+me\s+what\s+(?:happened|you\s+did))", re.I)
# Tools that only look. Everything else changes something (files, apps,
# sites, other AIs, jobs) and waits until the user asks for it.
_LOOK_ONLY_TOOLS = {"read_file", "list_directory", "search_web", "get_memory_context",
                    "search_vault_semantic", "read_vault_note", "recall_facts",
                    "search_chatgpt_history", "browser_status", "list_installed_apps",
                    "job_control"}


def _held_for_question(name: str, args: dict, last_user_text: str) -> str | None:
    """The tool result to give instead of running `name`, or None to run it."""
    looks = name in _LOOK_ONLY_TOOLS and not (
        name == "job_control" and str((args or {}).get("action", "")).lower() == "stop")
    if looks or not _REPORT_ASK_RE.match(last_user_text or ""):
        return None
    print(f"[POLICY] {name} held - the user asked what happened, not for an action: "
          f"{last_user_text[:70]!r}", flush=True)
    return ("[Held] The user only asked what happened, so nothing was run or changed. "
            "Answer from what you already know, in plain words, and offer to do it "
            "if they want.")


def execute_tool(name: str, args: dict, last_user_text: str = "") -> str:
    """Execute a tool by name with arguments."""
    from cygnus import tools
    held = _held_for_question(name, args, last_user_text)
    if held:
        return held
    return tools.execute_tool(name, _apply_consent_policy(name, args, last_user_text))


def _safe_json(raw) -> dict:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}




class JarvisBrain:
    """Multi-backend brain: Cerebras (primary) -> 9router -> Ollama -> mock (last resort)."""

    def __init__(self):
        # Gemini is optional: its key is reserved for the user's portfolio work,
        # so JARVIS must run without it. Only build the Gemini client when a key
        # is present; the brain chain skips Gemini entirely when it is absent.
        self.client = None
        if GEMINI_API_KEY:
            self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.model = GEMINI_MODEL
        self.conversation = []
        self._last_model_error = None
        self._use_router = (not JARVIS_LOCAL_ONLY) and (JARVIS_USE_9ROUTER)
        self._use_groq = (not JARVIS_LOCAL_ONLY) and (JARVIS_USE_GROQ and bool(GROQ_API_KEY))
        self._use_cerebras = (not JARVIS_LOCAL_ONLY) and (JARVIS_USE_CEREBRAS and bool(CEREBRAS_API_KEY))
        self._use_ollama = JARVIS_USE_OLLAMA
        self._prefer_local = JARVIS_PREFER_LOCAL and JARVIS_USE_OLLAMA
        # Local-only: no cloud tier is even attempted, and Gemini (which the
        # chain otherwise always falls through to) is skipped as well.
        self._local_only = JARVIS_LOCAL_ONLY and JARVIS_USE_OLLAMA
        if self._local_only:
            self._prefer_local = True
        # Counter, NOT a mutex. This only tells the keep-warm pinger to skip its
        # tick while a real turn is generating. It was a Lock, and a turn that
        # hung inside a tool (a wedged browser call) held it forever, so every
        # later turn blocked on acquire and JARVIS stopped answering anything at
        # all — including questions that never touch the local model. A counter
        # cannot deadlock: nobody ever waits on it.
        self._ollama_active = 0
        if self._use_ollama:
            where = ("LOCAL ONLY - no cloud fallback" if self._local_only
                     else ("primary" if self._prefer_local else "last resort before \"no model answered\""))
            print(f"[JARVIS] Ollama backend enabled ({OLLAMA_MODEL}, {where}).", flush=True)
            # Preload only when the local model is the brain (or is kept warm
            # on purpose). As a last-resort fallback it stays cold: loading
            # 3.9 GB at every start pushed a busy machine out of memory and
            # Windows killed JARVIS itself (2026-09-11). The first cold
            # fallback then costs ~47 s, which is the cheaper failure.
            if self._prefer_local or OLLAMA_KEEP_WARM:
                self._preload_ollama()
            else:
                print("[JARVIS] Ollama preload skipped (fallback only, keep-warm off).", flush=True)
        if self._use_groq:
            print(f"[JARVIS] Groq backend enabled ({GROQ_MODEL}).", flush=True)
        if self._use_cerebras:
            print(f"[JARVIS] Cerebras backend enabled ({CEREBRAS_MODEL}).", flush=True)
        self.last_backend = None
        # Per-turn telemetry for the HUD. Only fields the answering backend
        # actually reported are set, so the panel can leave a row blank instead
        # of inventing a number.
        self.last_stats = {}
        # Idle-reset clock: the last moment a turn started. A turn starting resets
        # it; after IDLE_RESET_MINUTES of silence the next turn clears context.
        self._last_turn_ts = time.time()

    @property
    def conversation(self):
        """This client's history. The desktop window, the phone and each test
        session keep their own (think(client=...)): one shared list let one
        client's pending question leak into another's answer (2026-09-12)."""
        return self.__dict__.setdefault("_conversations", {}).setdefault(_client_key(), [])

    @conversation.setter
    def conversation(self, value):
        self.__dict__.setdefault("_conversations", {})[_client_key()] = value

    def _maybe_reset_idle(self):
        """Full context reset when the conversation has been idle too long.

        The local 4b model degrades with accumulated history; after
        IDLE_RESET_MINUTES of silence the next turn starts fresh instead of
        replaying stale turns. 0 disables. Only fires on a real gap — active
        conversations keep their context.
        """
        if not IDLE_RESET_MINUTES:
            return
        # Not idle while a task it handed off is still running: the owner
        # waited out a 7-minute Claude job, came back to approve the next
        # step, and found the conversation wiped (2026-09-14).
        try:
            from cygnus.jobs import registry as jobs
            if jobs.active():
                return
        except Exception:
            pass
        idle = time.time() - self._last_turn_ts
        if idle > IDLE_RESET_MINUTES * 60:
            self.reset()
            print(f"[JARVIS] context reset after {idle/60:.1f} min idle", flush=True)

    def note_late_result(self, text, client=None):
        """A background job's answer joins the asking client's history, and
        counts as activity for the idle reset. It used to reach only the
        screen, so "I approved it! Proceed" had nothing to refer to and
        Cygnus asked what task was meant (2026-09-14)."""
        if not text:
            return
        _progress_local.client = client or "default"
        globals()["_LAST_CLIENT"] = client or "default"
        self.conversation.append({"role": "assistant",
                                  "content": "[A task I handed off finished. Its answer:]\n" + str(text)[-3000:]})
        self._cap_conversation()
        self._last_turn_ts = time.time()

    def _cap_conversation(self, keep: int = 60) -> None:
        """Bound the replayed history. The per-call replay paths trim their own
        copies (router) or not at all (cerebras/ollama), and this list used to
        grow for the whole process lifetime — after days of uptime every turn
        replayed thousands of stale messages. Cuts land on a user message so a
        tool result is never separated from the tool_calls block it answers."""
        if len(self.conversation) <= keep:
            return
        trimmed = self.conversation[-keep:]
        while trimmed and trimmed[0].get("role") != "user":
            trimmed.pop(0)
        self.conversation = trimmed

    def think(self, user_input: str, on_hermes_done=None, progress_cb=None, client=None,
              remote=False) -> str:
        """One turn, then its observers: the conversation state records it and
        the understand-first router decides in shadow (in the background,
        after the reply - never in its way). `client` names whose conversation
        the turn belongs to; every thread sets it before touching history."""
        _progress_local.client = client or "default"
        globals()["_LAST_CLIENT"] = client or "default"
        _progress_local.remote = bool(remote)        # the phone: it may look, not act
        _progress_local.user_text = user_input       # open_application reads "a new ..."
        started = None
        try:
            from cygnus.brain import intent_router
            started = intent_router.turn_started(user_input)
        except Exception:
            pass
        self._lead_record = None
        self._ask_remember = None
        self._gate = None
        self._shared_out = False
        _progress_local.grounded = None
        try:
            from cygnus.brain import command_chain
            clauses = command_chain.split_commands(user_input, is_target=_is_open_target)
        except Exception:
            clauses = [user_input]
        try:
            if len(clauses) > 1:
                reply = self._run_chain(clauses, on_hermes_done, progress_cb)
            else:
                reply = self._think_turn(user_input, on_hermes_done=on_hermes_done,
                                         progress_cb=progress_cb)
        finally:
            # The facts rode along for this one answer; history keeps the
            # user's own words so they are not re-sent on every later turn.
            grounded = getattr(_progress_local, "grounded", None)
            if grounded:
                msg, original = grounded
                msg["content"] = original
                _progress_local.grounded = None
        reply = self._with_remember_question(reply)
        if self._shared_out and isinstance(reply, str):     # the owner hears that it left the machine
            note = " I shared those recent steps with my answering model to explain them."
            if self.conversation and self.conversation[-1].get("content") == reply:
                self.conversation[-1]["content"] = reply + note
            reply += note
        if self._gate is not None:          # what the gate would do, next to what ran
            try:
                from cygnus.brain import action_gate
                action_gate.log_shadow(user_input, self._gate, {
                    "backend": self.last_backend, "intent": (self.last_stats or {}).get("intent"),
                    "asked": isinstance(reply, str) and _asks_something(reply)})
            except Exception:
                pass
        if started is not None:
            try:
                fixed = honest_reply(reply, intent_router._audit_since(started["audit_pos"]),
                                     (self.last_stats or {}).get("backend"), user_input)
                if fixed != reply:
                    if self.conversation and self.conversation[-1].get("content") == reply:
                        self.conversation[-1]["content"] = fixed
                    reply = fixed
            except Exception:
                pass
            try:
                intent_router.turn_finished(started, reply, backend=self.last_backend,
                                            stats=self.last_stats, decided=self._lead_record)
            except Exception as e:
                print(f"[shadow] observer failed: {type(e).__name__}", flush=True)
        return reply

    def _ground_running(self, text):
        """Attach the agents' state and recent steps (running_apps) to this
        turn's user message for the model to explain; think() puts the
        original back and tells the owner the steps left the machine."""
        if not self.conversation or self.conversation[-1].get("role") != "user":
            return
        try:
            from cygnus.apps import running as running_apps
            facts = running_apps.explain_context(running_apps.inventory(), running_apps.focus_of(text))
        except Exception:
            return
        if not facts:
            return
        msg = self.conversation[-1]
        _progress_local.grounded = (msg, msg["content"])
        msg["content"] = msg["content"] + "\n\n[What is running, read from this machine]\n" + facts
        self._shared_out = True

    def _with_remember_question(self, reply):
        """The reply, plus "Should I remember that …?" when the turn held a
        preference that only might be one and the reply asks nothing itself."""
        text = getattr(self, "_ask_remember", None)
        if not text or not isinstance(reply, str) or _asks_something(reply):
            return reply
        question = remember_question(text)
        if not question:
            return reply
        asked = reply.rstrip() + " " + question
        if self.conversation and self.conversation[-1].get("content") == reply:
            self.conversation[-1]["content"] = asked
        return asked

    def _run_chain(self, clauses, on_hermes_done=None, progress_cb=None):
        """Each command of "open Spotify and play X" in turn, each routed on
        its own. Stops at one that asks for a confirm or a pick: the rest
        may depend on it."""
        replies = []
        for i, clause in enumerate(clauses):
            print(f"[chain] {i + 1}/{len(clauses)}: {clause!r}", flush=True)
            reply = self._think_turn(clause, on_hermes_done=on_hermes_done,
                                     progress_cb=progress_cb)
            replies.append(reply)
            if ("[NEEDS_CONFIRM" in reply or reply.startswith("[NEEDS_PICK]")) \
                    and i + 1 < len(clauses):
                replies.append("I'll wait on that before the rest: "
                               + "; ".join(clauses[i + 1:]) + ".")
                break
        return "\n".join(replies)

    def _ground_own_work(self):
        """Attach what JARVIS recently did to this turn's user message, for
        the model to answer from; think() puts the original text back."""
        if not self.conversation or self.conversation[-1].get("role") != "user":
            return
        msg = self.conversation[-1]
        facts = _own_work_facts(msg["content"])
        if not facts:
            return
        _progress_local.grounded = (msg, msg["content"])
        msg["content"] = msg["content"] + facts

    def _think_turn(self, user_input: str, on_hermes_done=None, progress_cb=None) -> str:
        """Route to Cerebras (primary); then 9router; then local Ollama; then mock.

        Each hop falls through on error or rate-limit (429/quota/rate) so a dead
        or throttled provider never blocks the request.

        `on_hermes_done` (Phase 4): when provided (a callable) and the turn routes
        to the Hermes harness, the delegation runs in the BACKGROUND — think()
        returns the "working" ack immediately and the real answer is delivered to
        on_hermes_done(result) when Hermes finishes. Used by the voice/WS path so
        complex tasks don't freeze the mic for the ~2-min Hermes cold start.

        `progress_cb`: when provided (a callable), tools may call it with short
        status strings mid-task ("Searching YouTube Music...", "Playing now...")
        so the WS handler can speak progress while the task is still running.
        """
        # Store progress callback in thread-local so tools can reach it.
        _progress_local.cb = progress_cb
        # A delegation the model starts takes the same late-answer path.
        from cygnus import tools as _tools_jobs
        _tools_jobs.set_job_callback(on_hermes_done)
        self._maybe_reset_idle()
        self._last_turn_ts = time.time()
        # A new user turn for the safety gate: a held action runs only on a
        # yes in a later turn, and a tool's confirm flag counts only then.
        from cygnus import safety as _safety
        _safety.begin_turn()
        self._last_model_error = None      # this turn's failure, not the last one's
        self.conversation.append({"role": "user", "content": user_input})
        self._cap_conversation()

        # The answer to the router's own "Should I ... in Spotify, sir?"
        # (an ability the user marked ask): yes runs it, no drops it.
        try:
            from cygnus.brain import intent_router as _ir
            _consent = _ir.answer_pending_ability(user_input)
            if _consent is not None:
                self.conversation.append({"role": "assistant", "content": _consent})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "ability_consent"}
                return _consent
        except Exception as e:
            print(f"[JARVIS] ability consent failed ({e}); routing normally...")

        # The answer to "Should I remember that …?" (think() asked it).
        try:
            _kept = answer_remember(user_input)
            if _kept is not None:
                self.conversation.append({"role": "assistant", "content": _kept})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "remember_consent"}
                return _kept
        except Exception as e:
            print(f"[JARVIS] remember consent failed ({e}); routing normally...")

        # The answer to "I'll stop ... Say confirm" (running_apps).
        try:
            _stopped = answer_running_stop(user_input)
            if _stopped is not None:
                self.conversation.append({"role": "assistant", "content": _stopped})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "running_stop_consent"}
                return _stopped
        except Exception as e:
            print(f"[JARVIS] running stop consent failed ({e}); routing normally...")

        # The reply to "Which one should I open?" or a clarify question: the
        # chosen request routes from here as if it had been said outright.
        try:
            _said, _chosen = answer_choice(user_input)
            if _said is not None:
                self.conversation.append({"role": "assistant", "content": _said})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "choice"}
                return _said
            if _chosen:
                print(f"[JARVIS] choice picked: {user_input!r} -> {_chosen!r}", flush=True)
                user_input = _chosen
                self.conversation[-1] = {"role": "user", "content": user_input}
        except Exception as e:
            print(f"[JARVIS] choice failed ({e}); routing normally...")

        # A no to the confirm JARVIS just asked for. Runs before the
        # correction stripper, which would turn "no, cancel that" into a new
        # task "cancel that". A multi-step rule's own question (and a rule's
        # "Which movie?") keeps its answer.
        if _BARE_DECLINE_RE.match((user_input or "").strip()):
            try:
                from cygnus.rules import steps as _rs
                from cygnus import tools as _td
                if not _rs.active() and not _td._PENDING_RULE_SLOT:
                    from cygnus import safety as _sf
                    _declined = _sf.decline_pending() or _td.decline_pending()
                    if _declined is not None:
                        self.conversation.append({"role": "assistant", "content": _declined})
                        self.last_backend = "instant"
                        self.last_stats = {"backend": "instant", "intent": "decline"}
                        return _declined
            except Exception as e:
                print(f"[JARVIS] decline failed ({e}); routing normally...")

        # The words as spoken, before the routing strip below turns "Cygnus?"
        # into "?" - the check-in test needs them.
        _spoken = user_input
        # Correction prefix / leading filler: "I mean visit X" and "Now visit
        # X" route exactly like "visit X". The conversation keeps the original
        # wording; only routing sees the stripped form. Search commands keep
        # the marker — parse_search_command reads it to replace the previous
        # query instead of starting a new one, and strips fillers itself.
        try:
            from cygnus import tools as _tc
            _routed = _tc.strip_fillers(_tc.strip_correction_prefix(
                _tc.strip_fillers(user_input)))
            if _routed != user_input and not _tc._SEARCH_START.match(_routed):
                print(f"[JARVIS] routing prefix stripped: {user_input!r} -> {_routed!r}")
                user_input = _routed
        except Exception:
            pass

        # Apps-panel action rules ("search movies Dune in brave" -> the rule's
        # site, in the rule's browser); also answers our own "Which movie,
        # sir?" follow-up before any other path can mistake the bare name for
        # a new request. Its last tier asks a model what a rule means; an
        # instant answer skips that tier, which made "what time is it" take
        # about 4 s (2026-09-22).
        try:
            from cygnus import tools as _tr
            _rule_out = _tr.try_rule_action(
                user_input,
                semantic=(fast_path_answer(user_input)[0] is None
                          and classify_intent(user_input) not in NO_RULE_GUESS_INTENTS))
        except Exception:
            _rule_out = None
        if _rule_out is not None:
            self.conversation.append({"role": "assistant", "content": _rule_out})
            self.last_backend = "instant"
            self.last_stats = {"backend": "instant", "intent": "rule_action"}
            return _rule_out

        # Phase 23: passive preference capture, a NON-BLOCKING side-effect. A
        # preference said outright is saved; one that only might be is asked
        # about after the answer (think() adds the question); requests,
        # corrections and questions are never saved.
        try:
            _pref_kind = None
            if classify_intent(user_input) not in ("remember", "recall_memory"):
                _pref_kind = preference_kind(user_input)
            if _pref_kind == "save":
                _pref_text = user_input.strip()
                def _capture_pref():
                    try:
                        from cygnus import tools as _tools
                        _tools.remember_fact(_pref_text)
                    except Exception:
                        pass
                _threading.Thread(target=_capture_pref, daemon=True).start()
            elif _pref_kind == "ask":
                self._ask_remember = user_input.strip()
        except Exception:
            pass

        # Each turn reports its own telemetry; clear last turn's so a backend that
        # records nothing cannot leave stale numbers on the HUD.
        self.last_stats = {}

        # Bare-confirm release: "confirm"/"proceed"/"go ahead" must release the
        # pending destructive task, not become a NEW gated task. The latch in
        # tools.py requires the exact task text, which a spoken confirm can
        # never match — so without this, confirms piled up as waiting-on-confirm
        # jobs forever. If nothing is pending, fall through to normal routing.
        if _BARE_CONFIRM_RE.match((user_input or "").strip()):
            try:
                from cygnus import tools as _t
                from cygnus import safety as _sf
                hermes_pending = _t.pending_confirm_task()
                auton_pending = _t.pending_autonomous_goal()
                out = None
                # An action the safety gate held, when it is the newest
                # question: that is the one the user just heard.
                _held_ts = _sf.pending_ts()
                if _held_ts and _held_ts >= max((_t._PENDING_HERMES_CALL or {}).get("ts") or 0.0,
                                                _t._PENDING_DESTRUCTIVE.get("autonomous_ts") or 0.0):
                    out = _sf.confirm_pending()
                    if out is not None:
                        self.conversation.append({"role": "assistant", "content": out})
                        self.last_backend = "safety-confirm"
                        self.last_stats = {"backend": "safety-confirm", "intent": "confirm"}
                        return out
                if auton_pending or hermes_pending:
                    hermes_ts = (_t._PENDING_HERMES_CALL or {}).get("ts") or 0.0
                    auton_ts = _t._PENDING_DESTRUCTIVE.get("autonomous_ts") or 0.0
                    # Release whichever latch is NEWEST — that's the one whose
                    # NEEDS_CONFIRM the user actually just heard.
                    if auton_pending and auton_ts >= hermes_ts:
                        out = _t.confirm_autonomous()
                        backend = "autonomous-confirm"
                    elif hermes_pending:
                        out = _t.confirm_pending(on_done=on_hermes_done, progress_cb=progress_cb,
                                                 background=(on_hermes_done is not None))
                        backend = "hermes-confirm"
                if out is not None:
                    self.conversation.append({"role": "assistant", "content": out})
                    self.last_backend = backend
                    self.last_stats = {"backend": backend, "intent": "confirm"}
                    return out
                # A confirm for a question asked on ANOTHER device: say so.
                # Without this the stray "yes" was routed as a fresh request
                # (2026-09-23: a desktop "yes" launched a delete the phone had
                # been asked about).
                other = _sf.held_elsewhere() or _t.pending_confirm_elsewhere()
                if other:
                    msg = ("That confirm was asked on another device, sir - answer it "
                           f"there: “{str(other)[:90]}”")
                    self.conversation.append({"role": "assistant", "content": msg})
                    self.last_backend = "instant"
                    self.last_stats = {"backend": "instant", "intent": "confirm_other_client"}
                    return msg
                # A confirm for a question that already timed out: say so,
                # rather than let the model guess what "yes" was about.
                late = _t.expired_confirm_reply()
                if late:
                    self.conversation.append({"role": "assistant", "content": late})
                    self.last_backend = "instant"
                    self.last_stats = {"backend": "instant", "intent": "confirm_expired"}
                    return late
            except Exception as e:
                print(f"[JARVIS] confirm-release failed ({e}); falling back...")


        # The model first, with Cygnus's own tools, in a loop (agent_loop,
        # AGENT_LOOP_PLAN.md). Instant intents and slash commands stay with
        # the routing below; so does any turn no model could take.
        try:
            from cygnus.brain import agent_loop as _al
            if _al.MODE in ("lead", "shadow") and _al.takes(user_input, classify_intent(user_input), _client_key()):
                _ctx = {"client": _client_key(), "user_text": user_input, "on_done": on_hermes_done,
                        "progress_cb": progress_cb, "remote": getattr(_progress_local, "remote", False)}
                if _al.MODE == "lead":
                    _out = _al.run(self, user_input, _ctx)
                    if _out is not None:
                        self.last_backend = "agent"
                        self.last_stats = {"backend": "agent", "intent": "agent", **_al.last_stats()}
                        return _out
                    print("[agent] no model took the turn; old routing", flush=True)
                else:
                    _al.shadow(self, user_input, _ctx)
        except Exception as e:
            print(f"[agent] failed ({type(e).__name__}: {e}); old routing", flush=True)

        # Bare-"open" clarify follow-up: the previous turn asked
        # OPEN_CLARIFY_Q, so this turn picks the branch. A bare choice ("an
        # app") gets the second half of the question; a choice that already
        # names the target ("a website, youtube") is rewritten to "open
        # youtube" and routed normally, so the answer actually reaches the
        # open path instead of being chatted at.
        from cygnus.brain import dialogue_state as _dq
        if _dq.pending("open_choice"):
            _dq.answered()
            _open_ans, _open_rewrite = resolve_open_choice(user_input)
            if _open_ans:
                self.conversation.append({"role": "assistant", "content": _open_ans})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "clarify_open"}
                return _open_ans
            if _open_rewrite:
                user_input = _open_rewrite
                self.conversation[-1] = {"role": "user", "content": user_input}

        # P1: fast path for trivial intents — answer locally with NO cloud call and
        # NO tool schema. Removes the ~4s 9router round-trip for simple tasks.
        ans, intent = fast_path_answer(user_input)

        # "test", "ok", "jarvis?" on their own: someone checking Cygnus is
        # listening, not a task. They used to cost a full model turn (48 s
        # with the online model down) before delegate() turned them away.
        # Read BEFORE the fast path: the routing strip leaves "Cygnus?" as "?",
        # which is blank input now, and "I didn't catch that" is the wrong
        # answer to someone who is checking the mic is live.
        _check_in = _check_in_for(_spoken, user_input) \
            if intent == "general" or ans == BLANK_ASK else None
        if not _check_in and ans is not None and intent in SIMPLE_INTENTS:
            if ans == OPEN_CLARIFY_Q:               # the next turn answers it
                _dq.ask("open_choice", ans, owner="brain")
            self.conversation.append({"role": "assistant", "content": ans})
            self.last_backend = "instant"
            self.last_stats = {"backend": "instant", "intent": intent}
            return ans
        if _check_in:
            reply = _check_in
            self.conversation.append({"role": "assistant", "content": reply})
            self.last_backend = "instant"
            self.last_stats = {"backend": "instant", "intent": "check_in"}
            return reply

        # Where the keywords found nothing specific, a confident semantic pick
        # for a route it earned leads (semantic_route.lead; JARVIS_SEMANTIC).
        _semantic_route = None
        if intent in ("general", "app_reference"):
            _semantic_route = _semantic_lead(user_input)
            if _semantic_route == "clarify":
                ask = "Sorry, I didn't catch that. Could you say it again?"
                self.conversation.append({"role": "assistant", "content": ask})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "semantic_clarify"}
                return ask
            if _semantic_route:
                print(f"[semantic] leads: {intent} -> {_semantic_route}", flush=True)
                intent = _semantic_route

        # "Play my favorites on Spotify": the user's Liked Songs, not a search
        # the model makes up ("lo-fi", 2026-09-11).
        if intent == "music":
            _fav = play_favorites(user_input)
            if _fav is not None:
                self.conversation.append({"role": "assistant", "content": _fav})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "music_favorites"}
                return _fav

        # P1.5 (Tiered Router — Phase 1): general + app-reference tasks go to the
        # Hermes harness (the EXECUTOR) instead of the cloud brain. Hermes has the
        # full machine toolkit + the capability manifest + grounded memory, so it is
        # the right owner for anything that touches the machine or names an app.
        # Simple/voice-fast intents still take the instant path above. On any error
        # (Hermes unreachable, spawn failure) we fall through to the cloud brain
        # rather than failing the turn — the tiered router degrades, it doesn't break.
        # NOTE (Phase 3): `report` and `launch` are JARVIS-specific side-routes
        # (compose_report / launch_project) that run on the LOCAL brain + signed-in
        # browser — they are deliberately NOT routed to Hermes, so they are excluded.
        # NOTE (Phase 6b): `music` is also a JARVIS side-route (play_music via
        # music_agent, which drives YouTube Music in Brave). Hermes has no play_music
        # and no GUI control, so music MUST stay local — EXCLUDED from the Hermes route
        # (it is intentionally absent from the set below).
        # Phase 16/18: VOICE JOB CONTROL + DURABLE MEMORY — deterministic LOCAL
        # routing so "what's the status?" / "stop the task" / "remember X"
        # answer instantly from local state without touching Hermes, the cloud
        # chain, or any tool-call round-trip. Mirrors the instant-path contract
        # (last_backend="instant").
        # "What's running?", "bring Chrome to the front", "stop the leftover
        # tail", "read the terminal", "describe the Chrome window"
        # (running_apps): answered here. A stop is only planned; it waits for
        # a confirm (answer_running_stop). From the phone only the list.
        if intent in ("running", "running_front", "running_stop", "running_text", "running_describe"):
            try:
                from cygnus.apps import running as running_apps
                out, plan = running_apps.handle(intent, user_input,
                                                remote=getattr(_progress_local, "remote", False))
                if plan:
                    from cygnus.brain import dialogue_state
                    dialogue_state.ask("running_stop", out, plan, owner="brain")
                self.conversation.append({"role": "assistant", "content": out})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": intent}
                return out
            except Exception as e:
                print(f"[JARVIS] running apps failed ({type(e).__name__}: {e}); falling back...")
        if intent in ("job_status", "job_stop", "remember"):
            try:
                if intent == "remember":
                    from cygnus.tools import remember_fact
                    out = remember_fact(user_input)
                else:
                    from cygnus.tools import job_control
                    out = job_control("status" if intent == "job_status" else "stop")
                self.conversation.append({"role": "assistant", "content": out})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": intent}
                return out
            except Exception as e:
                print(f"[JARVIS] local job/memory path failed ({e}); falling back...")
        # Phase 15: MULTI-STEP goals run as supervised autonomous jobs —
        # deterministic, not left to the cloud model's tool choice. Single-step
        # requests keep the normal delegate() path.
        # Conversational guard FIRST: pure chat/QA (no action verb — "who am
        # I?", "what is your codename?") must skip the executor entirely and
        # fall through to the cloud tiers for a ChatGPT-style direct answer.
        # Previously every general turn was delegated to Hermes, so a simple
        # question cost a "Delegating to Hermes: CONTEXT [PROFILE]..." ack and
        # a ~2-min background job.
        try:
            from cygnus.tools import is_conversational
            _conversational = is_conversational(user_input)
        except Exception:
            _conversational = False
        # A semantic lead to an action is acted on - but a question stays a
        # question, and a question about JARVIS's own work (below) wins.
        if _semantic_route in ("general", "app_reference") and \
                not _WH_QUESTION_RE.match(user_input or ""):
            _conversational = False
        # A question is answered, not handed to an agent: "which of those do I
        # visit most?" and "what is the start of Cygnus?" became Hermes jobs,
        # because visit and start read as actions (2026-09-12).
        if intent == "general" and _is_question_not_request(user_input):
            _conversational = True
        # "What coding agent did you use to create this website?" is answered
        # from the record by the cloud brain, never re-run as a new goal.
        if _asks_about_own_work(user_input):
            _conversational = True
            self._ground_own_work()
        # "Explain what Claude is doing": its recent steps ride along to the
        # answering model (the owner allowed it; think() says they were shared).
        if intent == "running_explain":
            _conversational = True
            self._ground_running(user_input)
        # Phase 5: the understand-first router leads where the old code was
        # about to delegate or guess. It runs an ability or a rule it is
        # confident about (asking first when the user marked it "ask");
        # anything else falls through to the routing below, unchanged.
        if intent in ("general", "app_reference") and not _conversational:
            try:
                from cygnus.apps import abilities as _aa
                from cygnus.brain import dialogue_state as _ds
                from cygnus.brain import intent_router as _ir
                if _ir.MODE == "lead" and not _fast_lane_opens(user_input) \
                        and _router_may_lead(user_input) and not _changes_things(user_input):
                    _apps = _aa.load_apps()["apps"]
                    _snap = _ds.snapshot()
                    if _ir.worth_asking(user_input, _snap, _apps):
                        _led, self._lead_record = _ir.route(user_input, _snap, _apps, llm=_lead_llm)
                        if _led is not None:
                            self.conversation.append({"role": "assistant", "content": _led})
                            self.last_backend = "router-lead"
                            self.last_stats = {"backend": "router-lead", "intent": intent}
                            return _led
            except Exception as e:
                print(f"[JARVIS] router lead failed ({type(e).__name__}: {e}); old routing...")
        # One decision before acting (action_gate, CONFIRM_CLARIFY_PLAN.md):
        # act, confirm, or ask which of several real choices was meant. In
        # shadow it only records what it would do; in lead it asks.
        if intent in ("general", "app_reference") and not _conversational:
            try:
                from cygnus.brain import action_gate as _ag
                if _ag.MODE in ("shadow", "lead"):
                    _sem = None
                    try:
                        from cygnus.brain import semantic_route as _sr
                        _sem = _sr.pick(user_input)[1]
                    except Exception:
                        pass
                    self._gate = _ag.decide(user_input, open_target=_open_target(user_input),
                                            semantic_score=_sem,
                                            router_confidence=(self._lead_record or {}).get("confidence"))
                    if _ag.MODE == "lead" and self._gate.kind == "clarify":
                        _q = _ag.ask(self._gate)
                        self.conversation.append({"role": "assistant", "content": _q})
                        self.last_backend = "instant"
                        self.last_stats = {"backend": "instant", "intent": "gate_clarify"}
                        return _q
            except Exception as e:
                print(f"[gate] failed ({type(e).__name__}: {e}); routing on", flush=True)
        if intent in _MULTISTEP_INTENTS and not _conversational \
                and _is_multistep_goal(user_input):
            try:
                from cygnus.tools import run_autonomous
                out = run_autonomous(user_input)
                if not out.startswith("[NEEDS_CONFIRM]"):
                    self.conversation.append({"role": "assistant", "content": out})
                    self.last_backend = "autonomous"
                    self.last_stats = {"backend": "autonomous", "intent": intent}
                    return out
                # destructive goal awaiting confirm -> surface it this turn
                self.conversation.append({"role": "assistant", "content": out})
                self.last_backend = "autonomous-confirm"
                return out
            except Exception as e:
                print(f"[JARVIS] autonomous launch failed ({e}); falling back...")
        if intent in ("general", "app_reference") and not _conversational:
            try:
                from cygnus.tools import delegate
                # Phase 4: if a delivery callback is supplied (voice/WS path), run
                # Hermes in the background so the mic stays live; think() returns the
                # "working" ack now and on_hermes_done receives the real answer later.
                # progress_cb (Phase 4) streams milestone status to the WS consumer.
                if on_hermes_done is not None:
                    hermes_out = delegate(
                        user_input, timeout=300, max_turns=15,
                        background=True, on_done=on_hermes_done,
                        progress_cb=_progress_local.cb)
                else:
                    hermes_out = delegate(
                        user_input, timeout=300, max_turns=15,
                        progress_cb=_progress_local.cb)
                # The answer from Hermes is surfaced verbatim (the "Hermes reports:"
                # label is now stripped in tools.py). The user hears the real answer.
                self.conversation.append({"role": "assistant", "content": hermes_out})
                self.last_backend = "hermes"
                self.last_stats = {"backend": "hermes", "intent": intent}
                return hermes_out
            except Exception as e:
                print(f"[JARVIS] Hermes delegation failed ({e}); falling back to cloud brain...")

        # STOP (music): JARVIS-local side-route - must NOT reach the Hermes harness
        # or any cloud brain, which have no Spotify/YouTube-Music control. Resolve the
        # right stop tool deterministically: a Spotify mention -> stop_spotify (desktop
        # app, via the MediaPlayPause key); otherwise -> stop_music (YouTube Music in
        # Brave). This runs before the cloud tiers so a stop is instant and local.
        # RESCAN: instant local tool — scan installed software, no cloud needed.
        if intent == "rescan":
            try:
                from cygnus.tools import rescan_applications
                res = rescan_applications()
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "rescan"}
                return res
            except Exception as e:
                print(f"[JARVIS] Rescan failed ({e}); falling back to cloud brain...")

        # APP INSTALL: instant local route — add an app to JARVIS's voice registry.
        # "add X to my apps" / "install X in JARVIS" / "make X available to JARVIS".
        if intent == "app_install":
            try:
                from cygnus.tools import install_app
                app_name = user_input
                # Pattern A: "add X to my apps" / "install X in jarvis" / "add X to jarvis"
                m = re.search(
                    r"^(please\s+)?(add|install)\s+(.+?)\s+(to\s+my\s+apps|in\s+(?:jarvis|cygnus)|to\s+(?:jarvis|cygnus))\b",
                    app_name, re.I)
                if m:
                    app_name = m.group(3)
                else:
                    # Pattern B: "make X available to jarvis" / "make X available in jarvis"
                    m = re.search(
                        r"^(please\s+)?make\s+(.+?)\s+available\s+(to\s+(?:jarvis|cygnus)|in\s+(?:jarvis|cygnus))\b",
                        app_name, re.I)
                    if m:
                        app_name = m.group(2)
                    else:
                        # Pattern C: "register X" / "add X to JARVIS apps" (keyword variant)
                        app_name = re.sub(
                            r"^(please\s+)?(add|install|register|make available)\s+",
                            "", app_name, flags=re.I).strip()
                        app_name = re.sub(
                            r"\s+(to\s+my\s+apps|in\s+(?:jarvis|cygnus)|to\s+(?:jarvis|cygnus)|in\s+(?:jarvis|cygnus))\b",
                            "", app_name, flags=re.I).strip()
                res = install_app(app_name)
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "app_install"}
                return res
            except Exception as e:
                print(f"[JARVIS] App install failed ({e}); falling back to cloud brain...")

        # APP UNINSTALL: instant local route — remove an app from Cygnus's voice registry.
        # "remove X from JARVIS" / "uninstall X from my apps" / "stop controlling X".
        if intent == "app_uninstall":
            try:
                from cygnus.tools import uninstall_app
                app_name = user_input
                # Pattern A: "remove X from jarvis" / "uninstall X from my apps"
                m = re.search(
                    r"^(please\s+)?(remove|uninstall|unregister)\s+(.+?)\s+from\s+(jarvis|cygnus|my\s+apps)\b",
                    app_name, re.I)
                if m:
                    app_name = m.group(3)
                else:
                    # Pattern B: "stop controlling X"
                    m = re.search(
                        r"^(please\s+)?stop\s+controlling\s+(.+?)\s*(?:in\s+(?:jarvis|cygnus))?\b",
                        app_name, re.I)
                    if m:
                        app_name = m.group(2)
                res = uninstall_app(app_name)
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "app_uninstall"}
                return res
            except Exception as e:
                print(f"[JARVIS] App uninstall failed ({e}); falling back to cloud brain...")

        # LIST INSTALLED: instant local route — list all apps in JARVIS's registry.
        if intent == "list_installed":
            try:
                from cygnus.tools import list_installed_apps
                res = list_installed_apps()
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "list_installed"}
                return res
            except Exception as e:
                print(f"[JARVIS] List installed failed ({e}); falling back to cloud brain...")
        # ADD_SITE: instant local route — write the entry into web_registry.json
        # and confirm. Deliberately ahead of WEB_BROWSE and with NO page fetch:
        # "add hackernews, news.ycombinator.com" asks for a registration, and
        # browsing the URL answered a question nobody asked while the registry
        # stayed empty.
        if intent == "add_site":
            try:
                from cygnus import tools as _tools_mod
                _tools_mod.set_progress_cb(_progress_local.cb)
                parsed = parse_add_site(user_input)
                if parsed:
                    site_name, site_url = parsed
                    _progress(f"Registering {site_name}...")
                    res = execute_tool("add_site",
                                       {"name": site_name, "url": site_url},
                                       user_input)
                    self.conversation.append({"role": "assistant", "content": res})
                    self.last_backend = "instant"
                    self.last_stats = {"backend": "instant", "intent": "add_site"}
                    return res
            except Exception as e:
                print(f"[JARVIS] add_site failed ({e}); falling back to cloud brain...")

        # LIST_SITES: question ABOUT the registry — answer from it, never freelance.
        if intent == "list_sites":
            try:
                from cygnus import tools as _tools_mod
                _tools_mod.set_progress_cb(_progress_local.cb)
                res = execute_tool("list_sites", {}, user_input)
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "list_sites"}
                return res
            except Exception as e:
                print(f"[JARVIS] list_sites failed ({e}); falling back to cloud brain...")

        # OPEN_SITE: "visit youtube.com" — open it from the site registry (or as a
        # raw URL) right here, never through the Hermes delegation.
        if intent == "open_site":
            try:
                from cygnus import tools as _tools_mod
                _tools_mod.set_progress_cb(_progress_local.cb)
                # "open github in brave": the browser is where, not what.
                # parse_open_site drops the phrase, so read it off the request.
                _, _bword = _tools_mod._split_browser(user_input)
                res = execute_tool("open_site", {"name": _open_target(user_input),
                                                 "browser": _tools_mod._browser_key(_bword)
                                                 if _bword else None},
                                   user_input)
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "open_site"}
                return res
            except Exception as e:
                print(f"[JARVIS] open_site failed ({e}); falling back to cloud brain...")

        # WEB_BROWSE: instant local route — token-cheap page read via oc CLI.
        # Runs before the Hermes delegation so reading a URL costs only the
        # local subprocess, not a 22s cloud round-trip. Falls back to Hermes
        # if the local read fails (e.g. login-walled page).
        if intent == "web_browse":
            try:
                # No local import of execute_tool — Python binds a name per FUNCTION,
                # not per block, so importing it anywhere inside think() made the
                # module-level 3-arg wrapper unreachable from EVERY route in this
                # function, including the close_app/stop ones further down.
                import re as _re3; from cygnus import tools as _tools_mod
                _tools_mod.set_progress_cb(_progress_local.cb)
                tu = (user_input or "").strip()
                m = _re3.search(r"https?://\S+", tu)
                if not m:
                    m = _re3.search(r"\b\w[\w-]*\.(com|org|net|io|dev|edu|gov|ph)\b", tu)
                url = m.group(0).rstrip(").,;") if m else tu
                mode = "raw" if _re3.search(r"\b(raw|markdown|full)\b", tu.lower()) else "open"
                _progress(f"Reading {url}...")
                res = execute_tool("web_browse", {"url": url, "mode": mode})
                if res.startswith("[Error]"):
                    raise RuntimeError(res)
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "web_browse"}
                return res
            except Exception as e:
                print(f"[JARVIS] web_browse local failed ({e}); falling back to Hermes...")

        # RECALL_MEMORY: instant local route — speak what JARVIS remembers about
        # the user from durable memory (Honcho context, falling back to profile).
        # No cloud call, no tool-call round-trip: deterministic, voice-friendly.
        # Runs before the general/app_reference Hermes delegation. On any failure
        # it falls through to the normal "general" path, which injects the
        # persistent memory and answers via the cloud brain.
        if intent == "recall_memory":
            try:
                # See web_browse above: no local execute_tool import in think().
                from cygnus import tools as _tools_mod
                _tools_mod.set_progress_cb(_progress_local.cb)
                _progress("Recalling what I remember...")
                res = execute_tool("get_memory_context", {"tokens": 4000})
                if res and "no memory context available" not in res and not res.startswith("[Error]"):
                    answer = _format_recall_for_speech(res)
                    self.conversation.append({"role": "assistant", "content": answer})
                    self.last_backend = "instant"
                    self.last_stats = {"backend": "instant", "intent": "recall_memory"}
                    return answer
                raise RuntimeError("empty/fallback")
            except Exception as e:
                print(f"[JARVIS] recall_memory local failed ({e}); falling back to profile...")

        # CLOSE_APP: instant local route — close a named application.
        if intent == "close_app":
            try:
                # NO local `from tools import execute_tool` here. That shadowed the
                # module-level 3-arg wrapper with tools.execute_tool (2 params), so
                # the 3-arg call below raised
                #   TypeError: execute_tool() takes 2 positional arguments but 3 were given
                # every single time. The bare `except` swallowed it and fell through
                # to the cloud brain, so this deterministic route never once ran —
                # and the call also skipped _apply_consent_policy, which the wrapper
                # is what applies.
                import re as _re2; from cygnus import tools as _tools_mod
                _tools_mod.set_progress_cb(_progress_local.cb)
                m = _re2.search(
                    r"\b(?:close|quit|exit|kill|shut down)\b\s+(?:the\s+|my\s+)*(.+)",
                    (user_input or "").lower())
                app_name = m.group(1).strip() if m else user_input
                # "force close notepad" / "kill notepad": the user accepts
                # losing unsaved work; a plain close never ends the process.
                force = bool(_re2.search(r"\b(?:force|kill)\b", (user_input or "").lower()))
                _progress(f"Closing {app_name}...")
                res = execute_tool("close_application", {"app": app_name, "force": force},
                                   user_input)
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "close_app"}
                return res
            except Exception as e:
                print(f"[JARVIS] close_app failed ({e}); falling back to cloud brain...")
        if intent == "stop":
            try:
                # Same shadowing bug as close_app above — see that comment.
                from cygnus import tools as _tools_mod
                _tools_mod.set_progress_cb(_progress_local.cb)
                _progress("Stopping music...")
                if re.search(r"\bspotify\b", (user_input or "").lower()):
                    res = execute_tool("stop_spotify", {}, user_input)
                else:
                    res = execute_tool("stop_music", {}, user_input)
                self.conversation.append({"role": "assistant", "content": res})
                self.last_backend = "instant"
                self.last_stats = {"backend": "instant", "intent": "stop"}
                return res
            except Exception as e:
                print(f"[JARVIS] stop failed ({e}); falling back to cloud brain...")

        return self._answer_with_models(user_input)

    def _answer_with_models(self, user_input: str) -> str:
        """The model chain - local when preferred, Cerebras, 9router, local -
        under one turn budget (TURN_BUDGET): each call's timeout is what the
        turn has left, and a turn that runs out says so instead of waiting
        minutes on a model that is down."""
        _progress_local.deadline = time.monotonic() + TURN_BUDGET
        # 0) Local first, only when explicitly preferred (offline / on-device).
        if self._prefer_local:
            try:
                result = self._think_ollama(user_input)
                if result:
                    self.last_backend = "ollama"
                    self.last_stats["backend"] = "ollama"
                    return result
            except Exception as e:
                self._last_model_error = e
                # Do not claim a cloud fallback that local-only mode forbids —
                # the log was the main reason it looked like JARVIS was still
                # reaching for Claude or Gemini.
                print(f"[JARVIS] Ollama unavailable ({e})"
                      + ("; local-only, not falling back." if self._local_only
                         else "; falling back to cloud..."))

        # 1) PRIMARY: Cerebras (OpenAI-compatible free tier).
        #    If it's rate-limited (429/quota) or otherwise down, fall through to
        #    9router instead of burning the request.
        if self._use_cerebras:
            try:
                result = self._think_cerebras(user_input)
                if result:
                    self.last_backend = "cerebras"
                    self.last_stats["backend"] = "cerebras"
                    return result
            except Exception as e:
                self._last_model_error = e
                err = str(e)
                if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                    print("[JARVIS] Cerebras rate-limited (429/quota); falling back to 9router...")
                else:
                    print(f"[JARVIS] Cerebras unavailable ({e}); falling back to 9router...")

        # 2) FALLBACK: 9router (local AI proxy, multi-model).
        if self._use_router:
            try:
                result = self._think_router(user_input)
                if result:
                    self.last_backend = "router"
                    self.last_stats["backend"] = "router"
                    return result
            except Exception as e:
                self._last_model_error = e
                err = str(e)
                if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                    print("[JARVIS] 9router rate-limited; falling back to local Ollama...")
                else:
                    print(f"[JARVIS] 9router/mimo unavailable ({e}); falling back to local Ollama...")

        # 3) LOCAL: Ollama — the last real backend. A small local
        #    model beats canned demo text, so it is kept as the last resort.
        # A second turn is not queued behind one already on the CPU model:
        # four stacked there each waited minutes (2026-09-12).
        if self._use_ollama and not self._prefer_local and not self._ollama_active:
            try:
                result = self._think_ollama(user_input)
                if result:
                    self.last_backend = "ollama"
                    return result
            except Exception as e:
                self._last_model_error = e
                print(f"[JARVIS] Ollama unavailable ({e}); no model left to try.")

        # No model answered. Say which way it failed - never a canned reply:
        # the old "demo mode" replied as if it had opened an app while nothing
        # ran at all (found by bench/chaos.py, 2026-09-23).
        reply = (_NO_MODEL_REPLY if _time_left() < MIN_CALL_SECONDS and not self._last_model_error
                 else _why_no_model(self._last_model_error))
        print(f"[JARVIS] no model answered: {reply}", flush=True)
        self.conversation.append({"role": "assistant", "content": reply})
        self.last_backend = "no-model"
        self.last_stats = {"backend": "no-model"}
        return reply
        self._mock_brain.conversation = self.conversation.copy()
        self.last_backend = "mock"
        return self._mock_brain.think(user_input)

    def _think_router(self, user_input: str) -> str:
        """Call 9router (OpenAI-compatible) with mimo model + tool loop."""
        from openai import OpenAI

        client = OpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY, max_retries=0)
        otools = _openai_tools()

        # Build OpenAI-style messages from shared conversation history
        messages = [{"role": "system", "content": JARVIS_SYSTEM}]
        for msg in self.conversation:
            if msg["role"] == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                # assistant turn may carry tool_calls (from a prior router turn)
                if msg.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                            } for tc in msg["tool_calls"]
                        ]
                    })
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                for tr in msg["content"]:
                    # The Gemini path records tool results with no tool_call_id and
                    # no matching assistant tool_calls entry. Replaying one as a
                    # tool-role message raised KeyError('tool_call_id'), which this
                    # loop reported as "backend unavailable" — so ONE Gemini tool
                    # turn poisoned the history and every later turn failed on all
                    # four OpenAI-compatible backends straight through to demo
                    # mode. Fold those into plain assistant text instead: the
                    # context survives and the protocol stays valid.
                    tcid = tr.get("tool_call_id")
                    if tcid:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tcid,
                            "content": tr.get("content", "")
                        })
                    else:
                        messages.append({
                            "role": "assistant",
                            "content": f"[{tr.get('name', 'tool')} result] {tr.get('content', '')}"
                        })

        # Retry across free-tier 9router models so a rate-limited (429) primary
        # hops to a fresh model instead of giving up on the turn. Separate
        # quotas mean throttling is usually per-model, not per-account.
        class _RateLimited(Exception):
            pass

        _router_ready()
        model_list = _router_models()
        last_err, failed = None, None
        for model in model_list:
            if failed:
                _announce_model_switch(failed, model)
            try:
                pending = []
                started = time.monotonic()
                for _round in range(10):
                    # P3: skip the ~2.7k-token tool schema (and the tool-decision step) for
                    # simple intents that can never need a tool. Tiered max_tokens too.
                    # Bound the replay by TOKENS, not just _cap_conversation's
                    # message count. Cloud windows are large but quality decays
                    # long before they fill.
                    messages = _trim_history(messages, ROUTER_HISTORY_TOKEN_BUDGET)
                    needs_tool = classify_intent(user_input) not in SIMPLE_INTENTS
                    tools_arg = otools if needs_tool else None
                    tool_choice_arg = "auto" if needs_tool else None
                    max_tokens_arg = 256 if not needs_tool else 1024
                    try:
                        response = client.chat.completions.create(
                            model=model,
                            messages=messages,
                            tools=tools_arg,
                            tool_choice=tool_choice_arg,
                            max_tokens=max_tokens_arg,
                            timeout=_call_timeout(FIRST_CALL_S if _round == 0 else 45),
                        )
                    except Exception as ce:
                        cs = str(ce)
                        from cygnus.brain import router_keeper as _rk
                        if _rk.is_connection_error(ce):
                            # Every other model goes through the same dead
                            # router: stop here, the caller says so.
                            raise _RouterDown(cs)
                        if "429" in cs or "rate" in cs.lower() or "quota" in cs.lower():
                            print(f"[JARVIS] 9router model {model} rate-limited; trying next...")
                            raise _RateLimited(cs)
                        raise

                    if not response.choices:
                        break
                    message = response.choices[0].message

                    if message.tool_calls:
                        assistant_block = {
                            "role": "assistant",
                            "content": message.content or "",
                            "tool_calls": [{
                                "id": tc.id,
                                "name": tc.function.name,
                                "arguments": _safe_json(tc.function.arguments)
                            } for tc in message.tool_calls]
                        }
                        pending.append(assistant_block)
                        messages.append({
                            "role": "assistant",
                            "content": message.content or "",
                            "tool_calls": [{
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                            } for tc in message.tool_calls]
                        })

                        for tc in message.tool_calls:
                            # Expose the progress callback to tools so they can stream
                            # mid-task status while think() is still running.
                            from cygnus import tools as _tools_mod
                            _tools_mod.set_progress_cb(_progress_local.cb)
                            result = execute_tool(tc.function.name, _safe_json(tc.function.arguments),
                                                  user_input)
                            # pending -> self.conversation (replayed on every later
                            # turn), so it gets the clipped copy. messages is this
                            # turn only and keeps the full result.
                            pending.append({
                                "role": "tool",
                                "content": [{
                                    "name": tc.function.name,
                                    "content": _truncate_for_history(result),
                                    "tool_call_id": tc.id
                                }]
                            })
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result
                            })
                        continue

                    text = _clean_for_speech((message.content or "").strip())
                    if not text:
                        break
                    pending.append({"role": "assistant", "content": text})
                    self.conversation.extend(pending)
                    _report_healthy(model, time.monotonic() - started)
                    self.last_stats["model"] = model
                    if failed:
                        self.last_stats["switched_from"] = failed
                    return text

                # No text and no tool call is a failure, not a reply: the cursor
                # models answer 200 with empty content (2026-09-12).
                last_err = "empty reply"
                failed = model
                _report_unhealthy(model, last_err)
                continue
            except _RateLimited as rl:
                # Keep the provider's words: they carry the reset time, which
                # is what the user wants to hear ("resets in 5m").
                last_err = f"rate-limited: {rl}"
            except (_OutOfTime, _RouterDown):
                raise
            except Exception as e:
                last_err = str(e)
            failed = model
            _report_unhealthy(model, last_err)
        # All 9router models failed (rate-limited or errored) -> caller falls
        # back to Ollama, and says what went wrong if Ollama also fails.
        if last_err:
            raise RuntimeError(f"all models failed: {last_err}")
        return None

    def _preload_ollama(self):
        """Load the local model into RAM in the background at startup.

        A cold turn costs ~47s measured; warm turns are ~4-7s. Doing it on a
        daemon thread means JARVIS boots immediately and the model is hot by
        the time a cloud tier fails over to it.

        This must issue the *same shape* of call the real path uses — chat, with
        the tool schemas attached. Preloading via /api/generate makes /api/ps
        report the model resident, but the first /v1 chat+tools request then
        reloads anyway and still pays the full ~47s (measured twice). Sending
        the real shape also leaves the ~2.7k-token tool prefix in Ollama's
        prompt cache, which is most of the warm-up win.
        """
        import threading
        import time

        def _warm_once():
            from openai import OpenAI
            OpenAI(base_url=OLLAMA_BASE_URL, api_key=OLLAMA_API_KEY) \
                .chat.completions.create(
                    model=OLLAMA_MODEL,
                    messages=[{"role": "system", "content": JARVIS_SYSTEM},
                              {"role": "user", "content": "ready"}],
                    tools=_openai_tools(),
                    tool_choice="auto",
                    max_tokens=1,
                    timeout=300,
                )

        def _load():
            try:
                _warm_once()
            except Exception as e:
                print(f"[JARVIS] Ollama preload skipped ({e}).", flush=True)
                return

            if not OLLAMA_KEEP_WARM:
                return
            # Re-ping inside Ollama's eviction window so an idle gap never costs
            # the user a cold rebuild. _ollama_active keeps this off the CPU while
            # a real turn is generating — the ping is cheap but not free, and
            # contending for the same 8 cores would slow the live answer.
            while True:
                time.sleep(OLLAMA_WARM_INTERVAL)
                if self._ollama_active:
                    continue
                try:
                    _warm_once()
                except Exception as e:
                    print(f"[JARVIS] Ollama keep-warm ping failed ({e}); will retry.")
            # Re-ping inside Ollama's eviction window so an idle gap never costs
            # the user a cold rebuild. _ollama_active keeps this off the CPU while
            # a real turn is generating — the ping is cheap but not free, and
            # contending for the same 8 cores would slow the live answer.
            while True:
                time.sleep(OLLAMA_WARM_INTERVAL)
                if self._ollama_active:
                    continue
                try:
                    _warm_once()
                except Exception:
                    pass  # transient: the next tick tries again

        threading.Thread(target=_load, daemon=True).start()

    def _think_ollama(self, user_input: str) -> str:
        """Call local Ollama (OpenAI-compatible) with tool loop."""
        # try/finally, never a blocking acquire: a wedged turn must not be able
        # to stop the next one from running.
        self._ollama_active += 1
        try:
            return self._think_openai_compat(
                user_input,
                base_url=OLLAMA_BASE_URL,
                api_key=OLLAMA_API_KEY,
                model=OLLAMA_MODEL,
                max_tokens=OLLAMA_MAX_TOKENS,
                timeout=OLLAMA_TIMEOUT,
            )
        finally:
            self._ollama_active = max(0, self._ollama_active - 1)

    def _think_openai_compat(self, user_input: str, base_url: str, api_key: str,
                             model: str, max_tokens: int = 1024,
                             timeout: int = 45) -> str:
        """Generic OpenAI-compatible chat+tool loop.

        The router/Groq/Cerebras methods below are three near-identical copies of
        this loop; they are left alone rather than migrated, so adding a backend
        cannot regress a working one. New backends should call this.
        """
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)
        otools = _openai_tools()

        messages = [{"role": "system", "content": JARVIS_SYSTEM}]
        for msg in self.conversation:
            if msg["role"] == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                if msg.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                            } for tc in msg["tool_calls"]
                        ]
                    })
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                for tr in msg["content"]:
                    # The Gemini path records tool results with no tool_call_id and
                    # no matching assistant tool_calls entry. Replaying one as a
                    # tool-role message raised KeyError('tool_call_id'), which this
                    # loop reported as "backend unavailable" — so ONE Gemini tool
                    # turn poisoned the history and every later turn failed on all
                    # four OpenAI-compatible backends straight through to demo
                    # mode. Fold those into plain assistant text instead: the
                    # context survives and the protocol stays valid.
                    tcid = tr.get("tool_call_id")
                    if tcid:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tcid,
                            "content": tr.get("content", "")
                        })
                    else:
                        messages.append({
                            "role": "assistant",
                            "content": f"[{tr.get('name', 'tool')} result] {tr.get('content', '')}"
                        })

        # Only this path is trimmed. The cloud backends have context windows
        # large enough that dropping history would lose the conversation for no
        # gain; the local model does not, and an overflowing prompt there is
        # exactly what makes tool calls stop happening after a long session.
        messages = _trim_history(messages)

        pending = []
        # Telemetry for the HUD. Tokens accumulate across the loop because a turn
        # that calls a tool makes several round trips, and the interesting number
        # is what the whole turn cost, not the last leg.
        import time as _time
        _t0 = _time.time()
        _prompt_tok = _eval_tok = 0
        _gen_secs = 0.0
        _tools_used = []

        for _ in range(10):
            _leg = _time.time()
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=otools,
                tool_choice="auto",
                max_tokens=max_tokens,
                timeout=_call_timeout(timeout),
            )
            _gen_secs += _time.time() - _leg
            _usage = getattr(response, "usage", None)
            if _usage:
                _prompt_tok = getattr(_usage, "prompt_tokens", 0) or _prompt_tok
                _eval_tok += getattr(_usage, "completion_tokens", 0) or 0

            if not response.choices:
                return None
            message = response.choices[0].message

            if message.tool_calls:
                pending.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": _safe_json(tc.function.arguments)
                    } for tc in message.tool_calls]
                })
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in message.tool_calls]
                })

                for tc in message.tool_calls:
                    _tool_t0 = _time.time()
                    result = execute_tool(tc.function.name, _safe_json(tc.function.arguments),
                                          user_input)
                    _tools_used.append({"name": tc.function.name,
                                        "ms": int((_time.time() - _tool_t0) * 1000)})
                    pending.append({
                        "role": "tool",
                        "content": [{
                            "name": tc.function.name,
                            "content": result,
                            "tool_call_id": tc.id
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
                continue

            text = _clean_for_speech((message.content or "").strip())
            if not text:
                return None
            pending.append({"role": "assistant", "content": text})
            self.conversation.extend(pending)
            self.last_stats = {
                "model": model,
                "prompt_tokens": _prompt_tok,
                "eval_tokens": _eval_tok,
                # Generation rate only: wall-clock would be diluted by tool
                # time and understate what the model is actually doing.
                "tok_per_sec": round(_eval_tok / _gen_secs, 1) if _gen_secs > 0 else None,
                "latency_ms": int((_time.time() - _t0) * 1000),
                "tools": _tools_used,
            }
            return text

        return None

    def _think_groq(self, user_input: str) -> str:
        """Call Groq (OpenAI-compatible) with tool loop. Same wiring as the
        9router path, just a different base_url/key/model."""
        from openai import OpenAI

        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not available")

        client = OpenAI(base_url=GROQ_BASE_URL, api_key=GROQ_API_KEY, max_retries=0)
        otools = _openai_tools()

        # Build OpenAI-style messages from shared conversation history
        messages = [{"role": "system", "content": JARVIS_SYSTEM}]
        for msg in self.conversation:
            if msg["role"] == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                if msg.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                            } for tc in msg["tool_calls"]
                        ]
                    })
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                for tr in msg["content"]:
                    # The Gemini path records tool results with no tool_call_id and
                    # no matching assistant tool_calls entry. Replaying one as a
                    # tool-role message raised KeyError('tool_call_id'), which this
                    # loop reported as "backend unavailable" — so ONE Gemini tool
                    # turn poisoned the history and every later turn failed on all
                    # four OpenAI-compatible backends straight through to demo
                    # mode. Fold those into plain assistant text instead: the
                    # context survives and the protocol stays valid.
                    tcid = tr.get("tool_call_id")
                    if tcid:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tcid,
                            "content": tr.get("content", "")
                        })
                    else:
                        messages.append({
                            "role": "assistant",
                            "content": f"[{tr.get('name', 'tool')} result] {tr.get('content', '')}"
                        })

        pending = []
        for _ in range(10):
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                tools=otools,
                tool_choice="auto",
                max_tokens=1024,
                timeout=_call_timeout(45),
            )

            if not response.choices:
                return None
            message = response.choices[0].message

            if message.tool_calls:
                assistant_block = {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": _safe_json(tc.function.arguments)
                    } for tc in message.tool_calls]
                }
                pending.append(assistant_block)
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in message.tool_calls]
                })

                for tc in message.tool_calls:
                    from cygnus import tools as _tools_mod
                    _tools_mod.set_progress_cb(_progress_local.cb)
                    result = execute_tool(tc.function.name, _safe_json(tc.function.arguments),
                                          user_input)
                    pending.append({
                        "role": "tool",
                        "content": [{
                            "name": tc.function.name,
                            "content": result,
                            "tool_call_id": tc.id
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
                continue

            text = _clean_for_speech((message.content or "").strip())
            if not text:
                return None
            pending.append({"role": "assistant", "content": text})
            self.conversation.extend(pending)
            return text

        return None

    def _think_cerebras(self, user_input: str) -> str:
        """Call Cerebras (OpenAI-compatible) with tool loop. Same wiring as the
        router/Groq paths, just a different base_url/key/model."""
        from openai import OpenAI

        if not CEREBRAS_API_KEY:
            raise RuntimeError("CEREBRAS_API_KEY not available")

        client = OpenAI(base_url=CEREBRAS_BASE_URL, api_key=CEREBRAS_API_KEY, max_retries=0)
        otools = _openai_tools()

        # Build OpenAI-style messages from shared conversation history
        messages = [{"role": "system", "content": JARVIS_SYSTEM}]
        for msg in self.conversation:
            if msg["role"] == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                if msg.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                            } for tc in msg["tool_calls"]
                        ]
                    })
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                for tr in msg["content"]:
                    # The Gemini path records tool results with no tool_call_id and
                    # no matching assistant tool_calls entry. Replaying one as a
                    # tool-role message raised KeyError('tool_call_id'), which this
                    # loop reported as "backend unavailable" — so ONE Gemini tool
                    # turn poisoned the history and every later turn failed on all
                    # four OpenAI-compatible backends straight through to demo
                    # mode. Fold those into plain assistant text instead: the
                    # context survives and the protocol stays valid.
                    tcid = tr.get("tool_call_id")
                    if tcid:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tcid,
                            "content": tr.get("content", "")
                        })
                    else:
                        messages.append({
                            "role": "assistant",
                            "content": f"[{tr.get('name', 'tool')} result] {tr.get('content', '')}"
                        })

        pending = []
        for _ in range(10):
            response = client.chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=messages,
                tools=otools,
                tool_choice="auto",
                max_tokens=1024,
                timeout=_call_timeout(45),
            )

            if not response.choices:
                return None
            message = response.choices[0].message

            if message.tool_calls:
                assistant_block = {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": _safe_json(tc.function.arguments)
                    } for tc in message.tool_calls]
                }
                pending.append(assistant_block)
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in message.tool_calls]
                })

                for tc in message.tool_calls:
                    from cygnus import tools as _tools_mod
                    _tools_mod.set_progress_cb(_progress_local.cb)
                    result = execute_tool(tc.function.name, _safe_json(tc.function.arguments),
                                          user_input)
                    pending.append({
                        "role": "tool",
                        "content": [{
                            "name": tc.function.name,
                            "content": result,
                            "tool_call_id": tc.id
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
                continue

            text = _clean_for_speech((message.content or "").strip())
            if not text:
                return None
            pending.append({"role": "assistant", "content": text})
            self.conversation.extend(pending)
            return text

        return None

    def _think_gemini(self, user_input: str) -> str:
        """Original Gemini path. User message is already in self.conversation."""
        max_iterations = 10
        for _ in range(max_iterations):
            try:
                contents = []
                for msg in self.conversation:
                    if msg["role"] == "user":
                        contents.append(types.Content(role="user", parts=[types.Part(text=msg["content"])]))
                    elif msg["role"] == "assistant":
                        contents.append(types.Content(role="model", parts=[types.Part(text=msg["content"])]))
                    elif msg["role"] == "tool":
                        for tool_result in msg["content"]:
                            contents.append(types.Content(role="user", parts=[
                                types.Part.from_function_response(
                                    name=tool_result["name"],
                                    response={"result": tool_result["content"]}
                                )
                            ]))

                config = types.GenerateContentConfig(
                    system_instruction=JARVIS_SYSTEM,
                    tools=[TOOLS],
                    temperature=0.3,
                    max_output_tokens=2048
                )

                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config
                )

                if response.candidates and response.candidates[0].content.parts:
                    function_calls = []
                    text_parts = []

                    for part in response.candidates[0].content.parts:
                        if part.function_call:
                            function_calls.append(part.function_call)
                        elif part.text:
                            text_parts.append(part.text)

                    if function_calls:
                        tool_results = []
                        for fc in function_calls:
                            result = execute_tool(fc.name, dict(fc.args), user_input)
                            # Deliberately no tool_call_id: Gemini records the
                            # assistant turn without a matching tool_calls entry,
                            # so an id here would make the replayed pair invalid
                            # for the OpenAI-compatible backends rather than just
                            # unparseable. The replay folds id-less results into
                            # assistant text instead, which is protocol-safe.
                            tool_results.append({
                                "name": fc.name,
                                "content": result
                            })

                        self.conversation.append({
                            "role": "assistant",
                            "content": "".join(text_parts) if text_parts else ""
                        })
                        for tr in tool_results:
                            self.conversation.append({
                                "role": "tool",
                                "content": [tr]
                            })
                        continue

                    text = _clean_for_speech("".join(text_parts))
                    self.conversation.append({"role": "assistant", "content": text})
                    return text

            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
                    raise  # let think() handle mock fallback
                return f"I encountered an error: {error_str}"

        return "I apologize, but I encountered an issue processing that request."

    def reset(self, client=None):
        """Clear conversation history (`client`'s, else this turn's) and the
        per-turn telemetry."""
        if client:
            _progress_local.client = client
        self.conversation = []
        self._last_model_error = None
        # Re-enabling the router unconditionally here would have quietly undone
        # local-only mode the first time the context was cleared, putting the
        # cloud back in the chain without anything on screen saying so.
        self._use_router = (not JARVIS_LOCAL_ONLY) and JARVIS_USE_9ROUTER
        self.last_backend = None
        self.last_stats = {}
        # /clear must also end the warm Hermes session: the executor resumes
        # its own long-lived session on the next task, which silently carried
        # the PREVIOUS conversation's context across a clear (verified live:
        # a codename given before /clear leaked into the next session).
        try:
            from cygnus.jobs.warm_harness import warm_reset
            warm_reset()
        except Exception as e:
            print(f"[JARVIS] warm session reset failed ({e}); brain context cleared anyway.")
        if hasattr(self, '_mock_brain'):
            self._mock_brain.reset()
        return "Memory cleared. Ready for new commands."
