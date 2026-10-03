"""The model first, with Cygnus's own tools, in a loop (AGENT_LOOP_PLAN.md).

Before this, 76 phrase rules and 23 hand-written branches decided a turn
before any model read it, and a command none of them claimed went to the
Hermes CLI, which has no screen. Here the model reads the request with the
tools in view, calls one, sees the result, and decides again, up to
MAX_ROUNDS under BUDGET_S. Behind JARVIS_AGENT_LOOP: off | shadow | lead.
Everything failing returns None and the old routing takes the turn.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from cygnus import paths

MODE = os.getenv("JARVIS_AGENT_LOOP", "off").strip().lower()
MAX_ROUNDS = int(os.getenv("JARVIS_AGENT_ROUNDS", "12"))
BUDGET_S = float(os.getenv("JARVIS_AGENT_BUDGET", "240"))
CALL_CAP_S = 45.0
ACK_AFTER_S = 8.0
SHADOW_PATH = paths.log("agent_shadow.jsonl")

# The old routing answers these instantly and well; the loop leaves them.
INSTANT_INTENTS = {
    "math", "greeting", "thanks", "time", "help", "stop", "music", "job_status", "job_stop",
    "remember", "recall_memory", "running_stop", "rescan", "app_install", "app_uninstall",
    "list_sites", "add_site", "report", "launch", "clarify_play", "clarify_search", "clarify_open",
    "view", "volume"}

_current_app: dict = {}                 # client -> the app in play (slug)
_records: dict = {}                     # client -> the last tasks' records, newest last
_last_stats: dict = {}
_lock = threading.Lock()

# Tools that change something; the rest only look.
_ACTING = {"app_step", "show_window", "open_app", "open_site", "delegate", "remember", "slash",
           "prompt_claude"}


def remember_slash(client: str, slug: str, said: str, result: str) -> None:
    """A "/app …" command's outcome joins the record, so "status?" covers it."""
    _remember_record(client, {"user": f"/{slug} {said}", "ran": [("slash", {"slug": slug}, result[:4000])],
                              "reply": result, "ts": time.time(), "ago": time.strftime("%H:%M")})
# "status?", "what did you do?", "did it work?": answered from the record, no tools.
_REPORT_ASK_RE = re.compile(
    r"^\s*(?:(?:please|cygnus|jarvis|hey|ok(?:ay)?|so|and|sir|now)[,\s]+)*"
    r"(?:status\??|what(?:'s|\s+is)?\s+(?:happened|the\s+status|going\s+on)|what\s+did\s+you\s+(?:do|change|make|send)"
    r"|did\s+(?:it|that)\s+(?:work|go\s+through|send|run)|what\s+went\s+wrong|why\s+did|how\s+did\s+(?:it|that)\s+go"
    r"|explain(?:\s+(?:it|that))?|summari[sz]e(?:\s+(?:it|that))?|recap|tell\s+me\s+what\s+(?:happened|you\s+did))"
    r"\s*[.!?]*\s*$", re.I)
# A closing line that denies work the record shows.
_DENIAL_RE = re.compile(r"\b(nothing\s+(?:was\s+)?(?:sent|ran|run|happened|done)|(?:i\s+)?(?:did\s+not|didn'?t)\s+"
                        r"(?:send|prompt|run|open|do)|instead\s+of\s+(?:prompting|sending|running)|no\s+prompt\s+was\s+sent)\b", re.I)


def is_report_ask(text: str) -> bool:
    return bool(_REPORT_ASK_RE.match(text or ""))


def last_record(client: str) -> dict | None:
    rows = _records.get(client or "default") or []
    return rows[-1] if rows else None


def _remember_record(client: str, rec: dict) -> None:
    rows = _records.setdefault(client or "default", [])
    rows.append(rec)
    del rows[:-5]


def _reply_of(result: str) -> str:
    """Hermes's words out of 'Hermes, to "…": <reply>'."""
    m = re.match(r'^\w[\w ]*, to "[^"]*(?:"|…")\s*:\s*(.*)$', result or "", re.S)
    return (m.group(1) if m else result or "").strip()


def report(ran: list, spoken: bool = False) -> str:
    """What was done, from the tool record and nothing else: the line every
    acting turn ends with, and what "status?" is answered from. On
    2026-09-15 the model's own closing line said "nothing was sent there"
    about a prompt the record shows it sent."""
    acted, said, looked = [], [], []          # acted: fragments; said: whole sentences a tool returned
    for name, args, result in ran:
        a = args or {}
        app = (a.get("app") or "").strip()
        if name == "app_step":
            do = str(a.get("do") or "")
            raw = a.get("args")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    raw = {"text": raw}
            raw = raw or {}
            if do == "new_session":
                acted.append(f"started a new {app.title()} session")
            elif do == "open_session":
                acted.append(f"opened the {app.title()} session “{raw.get('name', '')}”")
            elif do == "send":
                reply = _reply_of(result)
                if spoken and len(reply) > 600:
                    reply = reply[:597].rstrip() + "…"
                acted.append(f"sent “{raw.get('text', '')}” to {app.title()}, which replied: {reply}")
            else:
                said.append(_short_result(result))
        elif name in _ACTING:
            said.append(_short_result(result))
        else:
            looked.append({"whats_running": "what is running", "app_actions": f"{app.title()}'s actions",
                           "read_window": f"{app.title()}'s window", "describe_window": f"a picture of {app.title()}",
                           "list_tabs": f"{app.title()}'s tabs",
                           "search_web": "the web", "read_file": "a file", "list_directory": "a folder",
                           "recall": "my memory"}.get(name, name))
    if not acted and not said and not looked:
        return "I didn't run anything for that, sir."
    parts = []
    if acted:
        parts.append("I " + "; then ".join(acted) + ("" if acted[-1].endswith((".", "!", "?", "…")) else "."))
    parts += [s if s.endswith((".", "!", "?", "…")) else s + "." for s in said]
    if looked:
        parts.append("I looked at " + ", ".join(dict.fromkeys(looked)) + ".")
    return " ".join(parts)


def _record_leads(ran: list) -> bool:
    """The record replaces the model's closing words when content passed
    through: a message sent to an app, a hand-off, a memory write."""
    for name, args, _ in ran:
        if name in ("delegate", "remember"):
            return True
        if name == "app_step" and str((args or {}).get("do")) == "send":
            return True
    return False


def _short_result(result: str) -> str:
    r = (result or "").strip().replace("[NEEDS_CONFIRM", "asked you to confirm [")
    return r if len(r) <= 400 else r[:397].rstrip() + "…"


def verify(final: str, ran: list, asked_question: bool) -> str:
    """The model's closing text checked against the record: a denial of work
    that ran, or a claim of work that did not, is replaced by the record."""
    acted = [r for r in ran if r[0] in _ACTING]
    if acted and _DENIAL_RE.search(final or ""):
        return report(ran, spoken=True)
    from cygnus.brain import core as bg
    if not acted and bg._ACTION_CLAIM_RE.match(final or "") and not asked_question:
        return "I didn't do that yet, sir: nothing ran. Tell me which app or site, and I will."
    return final


_AGENT_WORDS = re.compile(r"\b(hermes|claude|codex|gemini|opencode)\b", re.I)


def _names_running_app(text: str) -> bool:
    """"prompt hermes: what day is it today?" names Hermes; the time rule
    must not take it (it did, 2026-09-15: nothing was sent)."""
    if _AGENT_WORDS.search(text):
        return True
    try:
        from cygnus.apps import commands as app_commands
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        return any(t["slug"] in words for t in app_commands.targets())
    except Exception:
        return False


def takes(text: str, intent: str, client: str | None = None) -> bool:
    """Whether the loop should take this turn."""
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return False
    try:
        from cygnus import tools
        if tools._is_not_a_task(t):
            return False
        # Destructive requests go to the old routing, whose confirm gates
        # latch the task: the loop has none, and once answered "delete the
        # file C:\...\old.txt" by opening PowerShell unconfirmed (2026-09-22).
        if tools._DESTRUCTIVE_RE.search(t):
            return False
    except Exception:
        pass
    # "What is the status?" after a task the loop ran is about that task,
    # not about Cygnus's background jobs ("Nothing is running", 2026-09-15).
    if is_report_ask(t) and last_record(client or "default"):
        return True
    if intent in INSTANT_INTENTS and not _names_running_app(t):
        return False
    return True


def last_stats() -> dict:
    return dict(_last_stats)


# ------------------------------------------------------------------ tools --

def _p(_tool, _desc, **props):
    required = [k for k, v in props.items() if not v.endswith("(optional)")]
    return {"type": "function", "function": {
        "name": _tool, "description": _desc,
        "parameters": {"type": "object", "required": required,
                       "properties": {k: {"type": "string", "description": v} for k, v in props.items()}}}}


TOOLS = [
    _p("whats_running", "What is running on this machine: agents, servers, apps, background programs, and "
       "Cygnus's own tasks. Use it before acting on an app you are not sure is open.",
       focus="one of claude, hermes, opencode, gemini, codex to focus on (optional)"),
    _p("app_actions", "The actions available in a running app, by its slug from whats_running "
       "(hermes, chrome, notepad, powerpoint...). Call this before app_step.", app="the app's slug"),
    _p("app_step", "Do one action in a running app: a 'do' from app_actions with its args. Hermes: "
       "new_session; open_session (name); send (text). Other apps: open_item (name) opens a chat, tab or "
       "list entry by the name the user said, plus their abilities and the user's rules.",
       app="the app's slug", do="the action's do", args="JSON object of the action's args (optional)"),
    _p("show_window", "Bring a running program's window to the front.", app="its name or slug"),
    _p("read_window", "The text a running program's window shows (terminals and editors read well).",
       app="its name or slug"),
    _p("list_tabs", "The tabs open in a running program, window by window, by title (Brave, Chrome, Edge, "
       "Windows Terminal, Notepad). A window's title names only the tab in front, so use this for any "
       "question about tabs.", app="its name or slug"),
    _p("describe_window", "A picture of the window described by a vision model; say the picture left the machine.",
       app="its name or slug"),
    _p("open_app", "Open an installed application (brings it forward if already open).", name="the app's name"),
    _p("open_site", "Open a website in the browser.", name="a site name or address like facebook.com"),
    _p("prompt_claude", "Type a prompt into a Claude Code session already open in the terminal - the "
       "owner's main way of working - and say Claude's answer when it finishes. For 'tell / ask Claude "
       "(in <folder>) to ...'. It is read back and sent a few seconds later unless the owner says cancel. "
       "Prefer it over delegate whenever whats_running shows a Claude Code session.",
       session="the session's folder or title from whats_running, e.g. my-app (optional when only one is open)",
       prompt="what to type to Claude, in the owner's own words"),
    _p("delegate", "Hand a bigger job to a coding agent as a background task; the user is asked to confirm "
       "when it may change files, and the answer comes later.",
       agent="claude, codex, gemini or hermes", task="the job, in full"),
    _p("search_web", "Search the web.", query="the query"),
    _p("read_file", "Read a file.", path="the path"),
    _p("list_directory", "List a folder.", path="the path"),
    _p("recall", "What Cygnus remembers about the user that matches.", query="what to look for"),
    _p("remember", "Save a fact about the user.", fact="the fact"),
]

_APP_TOOLS = {"app_actions", "app_step", "show_window", "read_window", "describe_window", "list_tabs"}
# Tools that do not pass through execute_tool, which audits the rest itself.
# Without these lines the audit log (and the smoke test that reads it) never
# saw a garbled sentence open a Hermes session and bring it forward (2026-09-22).
_AUDITED = _APP_TOOLS | {"whats_running", "remember", "prompt_claude"}


# "tell Claude in my-app to" - the words before the prompt itself.
_OWNER_PROMPT_RE = re.compile(
    r"^\W*(?:(?:hey\s+)?cygnus\W+)?(?:please\s+)?(?:tell|ask|prompt|have|instruct)\s+(?:the\s+)?claude"
    r"(?:\s+code)?(?:\s+(?:in|on|for|at)\s+(?:the\s+)?[\w.-]+(?:\s+(?:project|folder|session))?)?"
    r"\s*(?:to\s+|that\s+|:\s*|,\s*)?", re.I)


def _target(app: str):
    from cygnus.apps import commands as app_commands
    slug = (app or "").strip().lower().lstrip("/")
    ts = app_commands.targets()
    hit = next((t for t in ts if t["slug"] == slug), None) or \
        next((t for t in ts if slug and (slug in t["name"].lower() or slug in t["slug"])), None)
    return hit, ts


def _run_tool(name: str, args: dict, ctx: dict) -> str:
    from cygnus.brain import core as bg
    from cygnus import tools
    user_text = ctx.get("user_text", "")
    if name == "whats_running":
        from cygnus.apps import running as running_apps
        return running_apps.summary(running_apps.inventory(use_cache=False), focus=args.get("focus") or None)
    if name in _APP_TOOLS:
        from cygnus.apps import commands as app_commands
        from cygnus.apps import running as running_apps
        target, ts = _target(args.get("app", ""))
        if not target:
            names = ", ".join(f"{t['slug']} ({t['name']})" for t in ts) or "nothing with a window"
            return f"[Error] No running app matches {args.get('app')!r}. Running now: {names}."
        _current_app[ctx.get("client", "default")] = target["slug"]
        rows = running_apps.inventory()
        row = next(r for r in rows if r["id"] == target["row"])
        if name == "app_actions":
            if not target["driver"]:
                return (f"{target['name']} has no actions or rules yet; only show_window, read_window, "
                        "describe_window, list_tabs.")
            sessions = app_commands.HermesWindow(row).session_titles() if target["driver"] == "hermes-window" else ()
            acts = app_commands.actions(target, row, app_commands._apps(), sessions)
            return json.dumps({"app": target["name"], "slug": target["slug"], "actions": acts}, ensure_ascii=False)
        if name == "app_step":
            if not target["driver"]:
                return f"[Error] {target['name']} has no actions I can run."
            sessions = app_commands.HermesWindow(row).session_titles() if target["driver"] == "hermes-window" else ()
            acts = app_commands.actions(target, row, app_commands._apps(), sessions)
            raw = args.get("args") or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    raw = {"text": raw}
            step = app_commands._step_from({"do": args.get("do"), "args": raw}, {a["do"]: a for a in acts})
            if not step:
                return f"[Error] {args.get('do')!r} is not one of {target['name']}'s actions. Call app_actions."
            # An ability marked "ask" waits in the dialogue; an open_item on
            # words like "Delete" waits in the safety gate below instead.
            if step.get("ask") and step.get("op") in ("ability", "rule"):
                from cygnus.brain import dialogue_state
                q = f"Should I {step['label'].lower()} in {target['name']}, sir?"
                dialogue_state.ask("ability", q, payload={"app": step["app"], "id": step["id"], "args": step["args"]})
                return "[Held] " + q
            # The safety gate (safety.py): text for Hermes that may change
            # things, and typing or keys in a terminal, wait for a yes.
            from cygnus import safety
            decision = safety.assess_app_step(target["slug"], {**step, "do": args.get("do")})
            return safety.gate(f"app_step {target['slug']} {args.get('do')} {json.dumps(raw)[:160]}", decision,
                               lambda: app_commands.run(target["slug"], [step], rows=rows))
        if name == "list_tabs":
            return running_apps.tabs_summary(target["name"], running_apps.window_tabs(row))
        intent ={"show_window": "running_front", "read_window": "running_text",
                  "describe_window": "running_describe"}[name]
        phrase = {"running_front": f"show {target['name']}", "running_text": f"read the {target['name']} window",
                  "running_describe": f"describe the {target['name']} window"}[intent]
        return running_apps.handle(intent, phrase, remote=bool(ctx.get("remote")))[0]
    if name == "open_app":
        return bg.execute_tool("open_application", {"app": args.get("name", "")}, user_text)
    if name == "open_site":
        return bg.execute_tool("open_site", {"name": args.get("name", "")}, user_text)
    if name == "prompt_claude":
        from cygnus.apps import claude_prompt
        text = (args.get("prompt") or "").strip() or user_text
        # As for delegate: the owner's words when the model's rewrite dropped
        # a negation, file name or number from them.
        if user_text and text != user_text:
            from cygnus.brain import task_brief
            if task_brief.dropped_constraints(user_text, text):
                text = _OWNER_PROMPT_RE.sub("", user_text, count=1).strip() or user_text
        try:
            session = claude_prompt.pick(args.get("session"))
            return claude_prompt.schedule(session, text, on_done=ctx.get("on_done"))
        except claude_prompt.PromptError as e:
            return f"[Error] {e}"
    if name == "delegate":
        agent = (args.get("agent") or "hermes").strip().lower()
        task = (args.get("task") or user_text).strip()
        # The model's rewrite of the job is checked against the owner's words:
        # a negation, file name, number or quoted string it dropped ("don't
        # touch the tests") was gone for good before 2026-09-24. When one is
        # missing the owner's own words go instead, minus "delegate this to X".
        if user_text and task != user_text:
            from cygnus.brain import task_brief
            lost = task_brief.dropped_constraints(user_text, task)
            if lost:
                _, _, cleaned = tools._explicit_cli_agent(user_text)
                print(f"[agent] delegate: the model's task dropped {lost!r}; using the owner's words", flush=True)
                from cygnus import audit
                audit.log_call("agent.delegate.dropped", {"model_task": task[:300], "lost": lost}, 0.0,
                               (cleaned or user_text)[:300])
                task = (cleaned or user_text).strip()
        if agent in ("claude", "codex", "gemini"):
            return tools._cli_delegate(task, agent, raw_task=user_text, on_done=ctx.get("on_done"))
        return tools.delegate_to_hermes(task, raw_task=user_text, background=ctx.get("on_done") is not None,
                                        on_done=ctx.get("on_done"), progress_cb=ctx.get("progress_cb"))
    if name in ("search_web", "read_file", "list_directory"):
        return bg.execute_tool(name, args, user_text)
    if name == "recall":
        return bg.execute_tool("recall_facts", {"query": args.get("query", "")}, user_text)
    if name == "remember":
        return tools.remember_fact(args.get("fact", ""))
    return f"[Error] Unknown tool: {name}"


# ------------------------------------------------------- checking results --

_MARKUP_RE = re.compile(r"<(?:atem:|antml:)?(?:function_calls|invoke|parameter)\b.*?(?:</(?:atem:|antml:)?function_calls>|$)",
                        re.S | re.I)
_OTHER_NAME_RE = re.compile(r"^\s*([A-Z][A-Z]{2,})\s*[—\-–:,]", re.M)


def tidy_agent_output(text: str, agent: str = "the agent") -> str:
    """An agent's answer as Cygnus may relay it: tool markup cut, a message
    addressed to someone else named for what it is."""
    text = text or ""
    cut = _MARKUP_RE.sub("", text).strip()
    if not cut and text.strip():
        return f"{agent} came back with tool markup and no answer, sir; it didn't finish the job."
    m = _OTHER_NAME_RE.search(cut)
    if m and m.group(1) not in ("OK", "PONG", "READY", "TODO", "NOTE"):
        cut = f"{agent} answered as if to {m.group(1).title()}, and asks: " + cut[m.end():].strip()
    return cut


def _clean(text: str) -> str:
    from cygnus.brain import core as bg
    return bg._clean_for_speech((text or "").strip())


# ---------------------------------------------------------------- the loop --

_SYSTEM = """You are Cygnus, a voice assistant on the owner's Windows laptop. Address them as sir; answer in one to three short spoken sentences, no markup or lists.
Work like an agent: read what the user means, use a tool, look at what it returned, then decide the next step. Check results; if something failed, try one other way, then say plainly what happened. Never say you did something no tool did. Ask only when a real choice is open.
The words come from speech recognition and can be misheard. When they do not make a clear request (a fragment, words that do not fit together), use no tool: ask the user to say it again, in one short sentence.
Running programs are addressed by slug (see whats_running). For a running app: app_actions first, then app_step with one of its actions. Hermes's actions are new_session, open_session (a title from the list), send (the user's words). Send in the session that is open unless the user asks for a new one or names another. An app with no action for the request: say what it can do and that a rule can be added in the Apps panel; do not improvise.
Tabs are not windows: a browser window's title is only its front tab. For how many tabs, or which, call list_tabs; never count them from whats_running or a window title.
Hand bigger jobs (code, files, research) to delegate; say the answer comes later.{extra}"""


# Characters of names each list may spend in the prompt, every turn.
_APPS_CAP = 1600
_SITES_CAP = 500


def _capped(names: list, cap: int) -> str:
    out, used = [], 0
    for n in names:
        if used + len(n) + 2 > cap:
            break
        out.append(n)
        used += len(n) + 2
    return ", ".join(out)


# A registry that is missing, half-written or hand-edited into another shape.
_REGISTRY_ERRORS = (ImportError, OSError, ValueError, TypeError, AttributeError)


def _app_names() -> list:
    """The owner's opted-in apps first, then store apps and Start-menu names
    ("task manager"); a bare exe stem (eppcchkr, tabtip32) is no name anyone
    says, and the opener still finds those by itself."""
    try:
        from cygnus.apps import curate
        apps = curate._apps()
        opted = [k for k in curate.registered_apps() if curate.is_enabled(k) and not curate.is_hidden(k)]
        rest = sorted(k for k in apps if k not in opted and not curate.is_hidden(k)
                      and (apps[k].get("kind") == "store" or " " in k))
    except _REGISTRY_ERRORS as e:
        print(f"[agent] app names left out of the prompt: {e}", flush=True)
        return []
    return opted + rest


def _site_names() -> list:
    """The saved sites, most visited first."""
    try:
        from cygnus.apps import web_registry
        sites = (web_registry.load_registry() or {}).get("sites", {})
        return sorted(sites, key=lambda k: -(sites[k].get("visits") or 0))
    except _REGISTRY_ERRORS as e:
        print(f"[agent] site names left out of the prompt: {e}", flush=True)
        return []


def _known_names() -> str:
    """The installed apps and saved sites, by the names open_app and open_site
    resolve. Before this the model saw only the running apps and guessed the
    rest. 166 of 412 open_application calls in logs/audit.jsonl failed, the top
    ones websites sent to the app opener (z chat 27, chatgpt 21, github 20);
    139 of them on 2026-09-11, before open_application learned to fall back to
    a site (a98bd8a). The opener copes now; the model still needs the names."""
    extra = ""
    apps, sites = _app_names(), _site_names()
    if apps:
        extra += "\nInstalled apps (open_app): " + _capped(apps, _APPS_CAP) + "."
    if sites:
        extra += "\nSaved sites (open_site): " + _capped(sites, _SITES_CAP) + "."
    if extra:
        extra += ("\nA name only among the sites goes to open_site, never open_app; a name in both "
                  "lists is the app unless the user says site or website.")
    return extra


def _system(ctx: dict) -> str:
    extra = ""
    try:
        from cygnus.apps import commands as app_commands
        ts = app_commands.targets()
        if ts:
            extra += "\nRunning now: " + ", ".join(f"{t['slug']} ({t['name']})" for t in ts[:12]) + "."
    except Exception:
        pass
    extra += _known_names()
    cur = _current_app.get(ctx.get("client", "default"))
    if cur:
        extra += f"\nThe app in play from the last turns: {cur}."
    # The app names come from the same transcripts the user's text does.
    return _safe(_SYSTEM.format(extra=extra))


def _safe(text: str) -> str:
    """Text a request body can carry. A lone surrogate (U+D800, from a broken
    UTF-16 boundary upstream) raised UnicodeEncodeError while the request was
    being encoded, on every one of the three router models, so the loop could
    not take that turn at all - a guaranteed bypass back to the old routing.
    Scrubbed here, before the message list is built, not caught after.

    collapse=False: this is the prompt path. Encoding safety is the whole
    reason to be here, and folding whitespace as well flattened the system
    prompt and every prior answer in the history onto one line."""
    try:
        from cygnus.brain import intake
        return intake.scrub(text or "", collapse=False)
    except Exception:
        return (text or "").encode("utf-8", "replace").decode("utf-8", "replace")


def _history(brain, n: int = 8) -> list:
    out = []
    for m in brain.conversation[-(n + 1):-1]:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            # The surrogate can arrive from the conversation too, not only from
            # this turn's text.
            out.append({"role": m["role"], "content": _safe(m["content"])[:1500]})
    return out


def _make_client():
    from openai import OpenAI
    from cygnus.brain import core as bg
    return OpenAI(base_url=bg.ROUTER_BASE_URL, api_key=bg.ROUTER_API_KEY or "x", max_retries=0)


# How many models a turn may walk. Three were not enough on 2026-09-22: the
# first hung and the next two were out of quota. Eight since Google's free
# tier joined (2026-09-27): its models refuse in well under a second once
# their daily quota is spent, and each has its own.
MAX_MODELS = 8


def _models() -> list:
    """9router's healthy models, then Google's; with no healthy 9router model,
    Google's first: the configured 9router ones then are the ones that failed
    (on 2026-09-26 all four answered 503 for days, ~2 s each, every turn)."""
    from cygnus.brain import core as bg
    from cygnus.brain import google_ai
    google = google_ai.models()
    health = bg._router_health()
    try:
        healthy = [m for m in (health.healthy_models() if health else []) if m]
    except Exception:
        healthy = []
    order = healthy + google if healthy else google + bg._router_models()
    return order[:MAX_MODELS]


def _client_for(model: str, router_client):
    """The client that serves `model`, and the name to send it."""
    from cygnus.brain import google_ai
    if google_ai.owns(model):
        return google_ai.client(), google_ai.wire_name(model)
    return router_client, model


def _call_echo(tc) -> dict:
    """A tool call as it goes back to the model on the next round. Gemini 3
    puts a thought signature in extra_content and refuses the next round
    without it ("Function call is missing a thought_signature", 2026-09-26);
    whatever extra the reply carried goes back as it came."""
    call = {"id": tc.id, "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
    extra = (getattr(tc, "model_extra", None) or {}).get("extra_content")
    if extra:
        call["extra_content"] = extra
    return call


def _failed(model: str, e: Exception) -> None:
    """A failed call rests the model where it lives: Google's here, 9router's
    in router_health (which probes by name through 9router)."""
    from cygnus.brain import core as bg
    from cygnus.brain import google_ai
    if google_ai.owns(model):
        google_ai.mark_failed(model, str(e))
    else:
        bg._report_unhealthy(model, str(e))


def run(brain, text: str, ctx: dict) -> str | None:
    """One agentic turn. The reply, or None when no model could take it."""
    from cygnus.brain import core as bg
    global _last_stats
    if not _lock.acquire(blocking=False):
        return None
    text = _safe(text)
    saved_deadline = getattr(bg._progress_local, "deadline", None)
    start = time.monotonic()
    deadline = start + BUDGET_S
    bg._progress_local.deadline = deadline
    client = _make_client()
    client_key = ctx.get("client", "default")
    # A question about what happened is answered from the record, no tools:
    # "status?" once became a prompt sent into a Hermes session (2026-09-15).
    asking = is_report_ask(text)
    if asking:
        # From the record, no model: asked what it did, the model once
        # answered with the running-apps list instead (2026-09-15). No
        # record this session: the old routing answers from the job log.
        rec = last_record(client_key)
        _lock.release()
        bg._progress_local.deadline = saved_deadline
        if not rec:
            return None
        final = f"The last thing I did, at {rec['ago']}, for “{rec['user'][:120]}”: {report(rec['ran'], spoken=True)}"
        _last_stats = {"model": None, "tools": [], "rounds": 0, "seconds": 0.0, "from": "record"}
        brain.conversation.append({"role": "assistant", "content": final})
        return final
    from cygnus.brain import google_ai
    router_down = False
    try:
        if not getattr(brain, "_use_router", True):
            # No router in this setup (CYGNUS_PROVIDER=gemini): nothing to
            # start, and its models are not asked.
            raise bg._RouterDown("no router configured")
        bg._router_ready()                  # starts a 9router that is down, bounded wait
    except bg._RouterDown as e:
        if not google_ai.models():
            print(f"[agent] {e}; old routing", flush=True)
            bg._progress_local.deadline = saved_deadline
            _lock.release()
            return None
        print(f"[agent] {e}; Google AI only", flush=True)
        router_down = True
    system = _system(ctx)
    tools_arg = TOOLS
    base = [{"role": "system", "content": system}] + _history(brain) + [{"role": "user", "content": text}]
    ran, acked, final, used, broke, waiting = [], False, None, None, None, False
    try:
        carry = None        # a turn a Google model began and hit its limit in
        for model in _models():
            if router_down and not google_ai.owns(model):
                continue                        # 9router is down: only Google can answer
            if carry is not None and not google_ai.owns(model):
                continue                        # a half-done turn passes only between Google models
            if google_ai.owns(model) and model not in google_ai.models():
                continue                        # rested since the list was made (a refused key rests all)
            messages = carry if carry is not None else list(base)
            api, wire = _client_for(model, client)
            try:
                for _round in range(MAX_ROUNDS):
                    left = deadline - time.monotonic()
                    if left < 3:
                        break
                    # The first call gets bg.FIRST_CALL_S: a model that has not
                    # answered by then hands over to the next one.
                    cap = bg.FIRST_CALL_S if _round == 0 else CALL_CAP_S
                    r = api.chat.completions.create(model=wire, messages=messages, tools=tools_arg,
                                                       tool_choice="auto" if tools_arg else None, max_tokens=700,
                                                       timeout=min(cap, left))
                    if not r.choices:
                        raise RuntimeError("empty reply")
                    msg = r.choices[0].message
                    if msg.tool_calls:
                        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                            _call_echo(tc) for tc in msg.tool_calls]})
                        stop = None
                        for tc in msg.tool_calls:
                            args = bg._safe_json(tc.function.arguments)
                            t0 = time.monotonic()
                            try:
                                out = _run_tool(tc.function.name, args, ctx)
                            except Exception as e:
                                out = f"[Tool Error] {tc.function.name}: {type(e).__name__}: {e}"
                            if not isinstance(out, str):
                                # recall_facts returns a list; .startswith on it ended
                                # the model's turn (2026-09-22). The model reads JSON.
                                out = json.dumps(out, ensure_ascii=False, default=str)
                            if tc.function.name in _AUDITED:
                                from cygnus import audit
                                audit.log_call("agent." + tc.function.name, args, time.monotonic() - t0, out)
                            if tc.function.name == "delegate":
                                out = tidy_agent_output(out, (args.get("agent") or "hermes").title())
                            ran.append((tc.function.name, args, out[:4000]))
                            # ascii(): a character the Windows console cannot encode
                            # raised inside print and ended the turn (2026-09-22).
                            print(f"[agent] {tc.function.name} {json.dumps(args)[:120]} -> {ascii(out[:120])}", flush=True)
                            messages.append({"role": "tool", "tool_call_id": tc.id, "content": out[:6000]})
                            if "[NEEDS_CONFIRM" in out or out.startswith("[Held] ") or out.startswith("[NEEDS_PICK]"):
                                stop = out
                        if not acked and ctx.get("progress_cb") and time.monotonic() - start > ACK_AFTER_S:
                            acked = True
                            try:
                                ctx["progress_cb"]("On it, sir.")
                            except Exception:
                                pass
                        if stop:                # the user must answer before anything else runs
                            final = stop.replace("[Held] ", "", 1)
                            waiting = True
                            break
                        continue
                    from cygnus.brain import router_health
                    err = router_health.provider_error(msg.content)
                    if err:                     # an error dressed as a reply: the next model answers
                        raise RuntimeError(err)
                    final = _clean(msg.content)
                    if not final:
                        raise RuntimeError("empty reply")
                    break
                if final is None:               # rounds or time spent
                    final = report(ran, spoken=True) + " I ran out of steps there, sir."
                used = model
                break
            except bg._OutOfTime:
                broke = "out of time"
                break
            except Exception as e:
                print(f"[agent] {model} failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
                _failed(model, e)
                broke = type(e).__name__
                if ran and google_ai.owns(model) and google_ai.can_hand_on(e):
                    # Work was done and the free quota ran out mid-turn: the
                    # next Google model finishes the same conversation, tool
                    # results and all, so nothing is done twice (2026-09-27:
                    # "Then my model stopped answering" after it had opened
                    # ChatGPT). Other Gemini models accept each other's turns.
                    carry = messages
                    continue
                if ran:                         # work was done: the record says so, not a next model
                    break
                from cygnus.brain import router_keeper
                if not google_ai.owns(model) and router_keeper.is_connection_error(e):
                    router_down = True          # the router went down: only Google can still answer
                continue
    finally:
        bg._progress_local.deadline = saved_deadline
        _lock.release()
    acted = [r for r in ran if r[0] in _ACTING]
    if not final and ran:                       # the model stopped after doing things: report the record
        final = report(ran, spoken=True) + " Then my model stopped answering, sir."
    if not final:
        return None
    asked = (text or "").rstrip().endswith("?") or asking
    if not waiting:                             # a confirm or pick question is passed on as it is
        final = verify(final, ran, asked)
        if _record_leads(ran) and not asking and not final.startswith(("I sent", "I started", "I opened", "I didn't")):
            # The record leads; the model's own closing words follow only when short.
            tail = final if len(final) <= 220 and not _DENIAL_RE.search(final) else ""
            final = report(ran, spoken=True) + (" " + tail if tail else "")
    _last_stats = {"model": used, "tools": [n for n, _, _ in ran], "rounds": len(ran),
                   "seconds": round(time.monotonic() - start, 1), "broke": broke}
    if not asking:
        _remember_record(client_key, {"user": text, "ran": ran, "reply": final, "ts": time.time(),
                                      "ago": time.strftime("%H:%M")})
    brain.conversation.append({"role": "assistant", "content": final})
    return final


# ------------------------------------------------------------------ shadow --

def shadow(brain, text: str, ctx: dict) -> None:
    """Off the turn's path: what the loop's first decision would be, logged
    next to what the old routing does, to compare before leading."""
    text = _safe(text)
    base = [{"role": "system", "content": _system(ctx)}] + _history(brain) + [{"role": "user", "content": text}]

    def work():
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "user": text, "first": None, "model": None}
        try:
            client = _make_client()
            for model in _models():
                try:
                    api, wire = _client_for(model, client)
                    r = api.chat.completions.create(model=wire, messages=base, tools=TOOLS,
                                                       tool_choice="auto", max_tokens=300, timeout=30)
                    msg = r.choices[0].message
                    rec["model"] = model
                    if msg.tool_calls:
                        tc = msg.tool_calls[0]
                        rec["first"] = {"tool": tc.function.name, "args": tc.function.arguments[:200]}
                    else:
                        rec["first"] = {"say": (msg.content or "")[:200]}
                    break
                except Exception as e:
                    rec["error"] = f"{model}: {type(e).__name__}"
        except Exception as e:
            rec["error"] = type(e).__name__
        try:
            rec["old_backend"] = getattr(brain, "last_backend", None)
            os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
            with open(SHADOW_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    threading.Thread(target=work, daemon=True, name="agent-shadow").start()
