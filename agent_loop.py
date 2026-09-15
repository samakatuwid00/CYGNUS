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

MODE = os.getenv("JARVIS_AGENT_LOOP", "off").strip().lower()
MAX_ROUNDS = int(os.getenv("JARVIS_AGENT_ROUNDS", "12"))
BUDGET_S = float(os.getenv("JARVIS_AGENT_BUDGET", "240"))
CALL_CAP_S = 45.0
ACK_AFTER_S = 8.0
SHADOW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "agent_shadow.jsonl")

# The old routing answers these instantly and well; the loop leaves them.
INSTANT_INTENTS = {
    "math", "greeting", "thanks", "time", "help", "stop", "music", "job_status", "job_stop",
    "remember", "recall_memory", "running_stop", "rescan", "app_install", "app_uninstall",
    "list_sites", "add_site", "report", "launch", "clarify_play", "clarify_search", "clarify_open"}

_current_app: dict = {}                 # client -> the app in play (slug)
_last_stats: dict = {}
_lock = threading.Lock()


def takes(text: str, intent: str) -> bool:
    """Whether the loop should take this turn."""
    t = (text or "").strip()
    if not t or t.startswith("/") or intent in INSTANT_INTENTS:
        return False
    try:
        import tools
        if tools._is_not_a_task(t):
            return False
    except Exception:
        pass
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
       "new_session; open_session (name); send (text). Other apps: their abilities and the user's rules.",
       app="the app's slug", do="the action's do", args="JSON object of the action's args (optional)"),
    _p("show_window", "Bring a running program's window to the front.", app="its name or slug"),
    _p("read_window", "The text a running program's window shows (terminals and editors read well).",
       app="its name or slug"),
    _p("describe_window", "A picture of the window described by a vision model; say the picture left the machine.",
       app="its name or slug"),
    _p("open_app", "Open an installed application (brings it forward if already open).", name="the app's name"),
    _p("open_site", "Open a website in the browser.", name="a site name or address like facebook.com"),
    _p("delegate", "Hand a bigger job to a coding agent as a background task; the user is asked to confirm "
       "when it may change files, and the answer comes later.",
       agent="claude, codex, gemini or hermes", task="the job, in full"),
    _p("search_web", "Search the web.", query="the query"),
    _p("read_file", "Read a file.", path="the path"),
    _p("list_directory", "List a folder.", path="the path"),
    _p("recall", "What Cygnus remembers about the user that matches.", query="what to look for"),
    _p("remember", "Save a fact about the user.", fact="the fact"),
]

_APP_TOOLS = {"app_actions", "app_step", "show_window", "read_window", "describe_window"}


def _target(app: str):
    import app_commands
    slug = (app or "").strip().lower().lstrip("/")
    ts = app_commands.targets()
    hit = next((t for t in ts if t["slug"] == slug), None) or \
        next((t for t in ts if slug and (slug in t["name"].lower() or slug in t["slug"])), None)
    return hit, ts


def _run_tool(name: str, args: dict, ctx: dict) -> str:
    import brain_gemini as bg
    import tools
    user_text = ctx.get("user_text", "")
    if name == "whats_running":
        import running_apps
        return running_apps.summary(running_apps.inventory(use_cache=False), focus=args.get("focus") or None)
    if name in _APP_TOOLS:
        import app_commands
        import running_apps
        target, ts = _target(args.get("app", ""))
        if not target:
            names = ", ".join(f"{t['slug']} ({t['name']})" for t in ts) or "nothing with a window"
            return f"[Error] No running app matches {args.get('app')!r}. Running now: {names}."
        _current_app[ctx.get("client", "default")] = target["slug"]
        rows = running_apps.inventory()
        row = next(r for r in rows if r["id"] == target["row"])
        if name == "app_actions":
            if not target["driver"]:
                return f"{target['name']} has no actions or rules yet; only show_window, read_window, describe_window."
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
            if step.get("ask"):
                import dialogue_state
                q = f"Should I {step['label'].lower()} in {target['name']}, sir?"
                dialogue_state.ask("ability", q, payload={"app": step["app"], "id": step["id"], "args": step["args"]})
                return "[Held] " + q
            return app_commands.run(target["slug"], [step], rows=rows)
        intent = {"show_window": "running_front", "read_window": "running_text",
                  "describe_window": "running_describe"}[name]
        phrase = {"running_front": f"show {target['name']}", "running_text": f"read the {target['name']} window",
                  "running_describe": f"describe the {target['name']} window"}[intent]
        return running_apps.handle(intent, phrase, remote=bool(ctx.get("remote")))[0]
    if name == "open_app":
        return bg.execute_tool("open_application", {"app": args.get("name", "")}, user_text)
    if name == "open_site":
        return bg.execute_tool("open_site", {"name": args.get("name", "")}, user_text)
    if name == "delegate":
        agent = (args.get("agent") or "hermes").strip().lower()
        task = (args.get("task") or user_text).strip()
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
    import brain_gemini as bg
    return bg._clean_for_speech((text or "").strip())


# ---------------------------------------------------------------- the loop --

_SYSTEM = """You are Cygnus, a voice assistant on the owner's Windows laptop. Address them as sir; answer in one to three short spoken sentences, no markup or lists.
Work like an agent: read what the user means, use a tool, look at what it returned, then decide the next step. Check results; if something failed, try one other way, then say plainly what happened. Never say you did something no tool did. Ask only when a real choice is open.
Running programs are addressed by slug (see whats_running). For a running app: app_actions first, then app_step with one of its actions. Hermes's actions are new_session, open_session (a title from the list), send (the user's words). An app with no action for the request: say what it can do and that a rule can be added in the Apps panel; do not improvise.
Hand bigger jobs (code, files, research) to delegate; say the answer comes later.{extra}"""


def _system(ctx: dict) -> str:
    extra = ""
    try:
        import app_commands
        ts = app_commands.targets()
        if ts:
            extra += "\nRunning now: " + ", ".join(f"{t['slug']} ({t['name']})" for t in ts[:12]) + "."
    except Exception:
        pass
    cur = _current_app.get(ctx.get("client", "default"))
    if cur:
        extra += f"\nThe app in play from the last turns: {cur}."
    return _SYSTEM.format(extra=extra)


def _history(brain, n: int = 8) -> list:
    out = []
    for m in brain.conversation[-(n + 1):-1]:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            out.append({"role": m["role"], "content": m["content"][:1500]})
    return out


def _make_client():
    from openai import OpenAI
    import brain_gemini as bg
    return OpenAI(base_url=bg.ROUTER_BASE_URL, api_key=bg.ROUTER_API_KEY or "x", max_retries=0)


def _models() -> list:
    import brain_gemini as bg
    return bg._router_models()[:3]


def run(brain, text: str, ctx: dict) -> str | None:
    """One agentic turn. The reply, or None when no model could take it."""
    import brain_gemini as bg
    global _last_stats
    if not _lock.acquire(blocking=False):
        return None
    saved_deadline = getattr(bg._progress_local, "deadline", None)
    start = time.monotonic()
    deadline = start + BUDGET_S
    bg._progress_local.deadline = deadline
    client = _make_client()
    base = [{"role": "system", "content": _system(ctx)}] + _history(brain) + [{"role": "user", "content": text}]
    ran, acked, final, used = [], False, None, None
    try:
        for model in _models():
            messages = list(base)
            try:
                for _ in range(MAX_ROUNDS):
                    left = deadline - time.monotonic()
                    if left < 3:
                        break
                    r = client.chat.completions.create(model=model, messages=messages, tools=TOOLS,
                                                       tool_choice="auto", max_tokens=700,
                                                       timeout=min(CALL_CAP_S, left))
                    if not r.choices:
                        raise RuntimeError("empty reply")
                    msg = r.choices[0].message
                    if msg.tool_calls:
                        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                            {"id": tc.id, "type": "function",
                             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls]})
                        stop = None
                        for tc in msg.tool_calls:
                            args = bg._safe_json(tc.function.arguments)
                            try:
                                out = _run_tool(tc.function.name, args, ctx)
                            except Exception as e:
                                out = f"[Tool Error] {tc.function.name}: {type(e).__name__}: {e}"
                            if tc.function.name == "delegate":
                                out = tidy_agent_output(out, (args.get("agent") or "hermes").title())
                            ran.append((tc.function.name, args, out[:300]))
                            print(f"[agent] {tc.function.name} {json.dumps(args)[:120]} -> {out[:120]!r}", flush=True)
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
                            break
                        continue
                    final = _clean(msg.content)
                    if not final:
                        raise RuntimeError("empty reply")
                    break
                if final is None:               # rounds or time spent: a plain summary, no more tools
                    r = client.chat.completions.create(
                        model=model, max_tokens=300, timeout=min(CALL_CAP_S, max(3, deadline - time.monotonic())),
                        messages=messages + [{"role": "user", "content":
                                              "Stop here. In one or two spoken sentences: what you did and what is left."}])
                    final = _clean(r.choices[0].message.content) if r.choices else ""
                used = model
                break
            except bg._OutOfTime:
                break
            except Exception as e:
                print(f"[agent] {model} failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
                bg._report_unhealthy(model, str(e))
                continue
    finally:
        bg._progress_local.deadline = saved_deadline
        _lock.release()
    if not final:
        return None
    if not ran and bg._ACTION_CLAIM_RE.match(final) and not (text or "").rstrip().endswith("?"):
        final = "I didn't do that yet, sir: nothing ran. Tell me which app or site, and I will."
    _last_stats = {"model": used, "tools": [n for n, _, _ in ran], "rounds": len(ran),
                   "seconds": round(time.monotonic() - start, 1)}
    brain.conversation.append({"role": "assistant", "content": final})
    return final


# ------------------------------------------------------------------ shadow --

def shadow(brain, text: str, ctx: dict) -> None:
    """Off the turn's path: what the loop's first decision would be, logged
    next to what the old routing does, to compare before leading."""
    base = [{"role": "system", "content": _system(ctx)}] + _history(brain) + [{"role": "user", "content": text}]

    def work():
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "user": text, "first": None, "model": None}
        try:
            client = _make_client()
            for model in _models():
                try:
                    r = client.chat.completions.create(model=model, messages=base, tools=TOOLS,
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
