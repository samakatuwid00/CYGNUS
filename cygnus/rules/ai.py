"""rules_ai.py - AI rule compiler for the JARVIS Apps panel.

Turns what the user typed - a command phrase plus what JARVIS should do, or a
single draft line holding both - into a structured rule JARVIS can execute:

    {"rule_id", "source_phrase", "intent", "summary", "enforcement",
     "adapter_check", "scope", "triggers": [...], "action": {...},
     "action_text", "compiled_by", "needs_clarification",
     "clarification_question"}

    action = {"type": "search_site" | "open_site" | "block" | "reminder",
              "site": "hollymoviehd.cc", "url": "https://hollymoviehd.cc",
              "search_url": "https://hollymoviehd.cc/?s={query}",
              "search_url_guessed": bool, "slot": "movie name",
              "browser": "brave", "applies_to": "launch"}

Setup-time only: runs when the user authors rules, never per spoken command
(rules_engine.match_action does the runtime side, with no model call).

Model chain: router -> local Ollama -> None. A dead router never blocks rule
setup, and callers fall back to rules_compiler's keyword parser when both
models are down. Guard: a site the user never typed is dropped - the model
may structure the user's words, not invent destinations.
"""

import json
import re

from cygnus.rules import engine as rules_engine

CONFIG_TIME_ONLY = True

ACTION_TYPES = ("search_site", "open_site", "steps", "block", "reminder")
RUNNABLE_TYPES = ("search_site", "open_site", "steps")
MAX_STEPS = 12
# Clicks that spend, send, remove or start playback always ask first.
_CONSEQUENTIAL_RE = re.compile(
    r"play|watch|stream|buy|pay|purchase|order|checkout|send|post|delete|remove|"
    r"submit|download|install|subscribe", re.I)

_BROWSER_KEYS = {"brave": "brave", "chrome": "chrome", "google chrome": "chrome",
                 "edge": "msedge", "msedge": "msedge", "microsoft edge": "msedge",
                 "firefox": "firefox"}
_BROWSER_LABELS = {"brave": "Brave", "chrome": "Chrome", "msedge": "Edge",
                   "firefox": "Firefox"}
_APPLIES_TO = {"launch": "pre_launch: ", "play": "pre_play: ",
               "close": "pre_close: ", "all": ""}

_URL_RE = re.compile(
    r"https?://[^\s'\"<>]+|(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,24}(?:/[^\s'\"<>]*)?",
    re.I)
_SEARCH_RE = re.compile(r"\b(search|find|look\s*up|look\s+for|query)\b", re.I)
_BLOCK_RE = re.compile(r"\b(never|block|don'?t|do\s+not|must\s+not|no\s+more)\b", re.I)
COMMAND_START_RE = re.compile(
    r"^(?:please\s+|(?:jarvis|cygnus)[,\s]+)*(search|find|look\s*up|look\s+for|open|launch|"
    r"visit|go\s+to|take\s+me\s+to|play|watch|browse|show)\b", re.I)
_WHEN_I_SAY_RE = re.compile(
    r"^\s*(?:when|if)\s+i\s+say\s+[\"']?(.+?)[\"']?\s*[,;]\s*(?:then\s+)?(.+)$", re.I)
_ARROW_RE = re.compile(r"\s*(?:->|=>|→)\s*")
_MUSIC_CHECK_RE = re.compile(
    r"^pre_play:\s*(skip if track\.explicit|cap \w[\w .-]* volume at \d{1,3}%)$", re.I)
_SPECIFIC_STOP = {"a", "an", "the", "some", "in", "on", "at", "for", "me", "my",
                  "please", "jarvis", "cygnus", "sir", "browser", "to", "up"}
_VERBS = {"search", "find", "look", "lookup", "open", "launch", "visit", "go",
          "take", "play", "watch", "browse", "show"}

_PROMPT = """You compile ONE user rule for the JARVIS voice assistant (the user calls it Cygnus).
The rule belongs to the app "{app}".{browser_note}
{context}
The user wrote:
COMMAND PHRASE: "{trigger}"
WHAT CYGNUS SHOULD DO: "{action}"{feedback}

Work out what the user really means - the goal behind the words, not the words - and return ONLY one JSON object:
{
  "type": "search_site" | "open_site" | "steps" | "block" | "reminder",
  "goal": one short sentence, in plain words, of what the user wants to happen,
  "trigger": the command phrase, cleaned up (lowercase, no filler words); for a block or reminder, a short name for it,
  "triggers": 3 to 6 other natural ways to say the same command, keeping its key words (the browser name and the kind of thing, e.g. "movies"),
  "site": the website domain the user wrote or clearly named (e.g. "example.com"; "youtube music" means "music.youtube.com"), or null,
  "search_url": for search_site, the site's search address with {query} where the search words go, or null if unsure,
  "slot": for search_site or a steps rule with a search or a typed message, a short name for what the user fills in (e.g. "movie name", "message"), or null,
  "sample": for search_site, one realistic example of what the user would fill in (e.g. "Inception" for a movie), or null,
  "browser": "brave" | "chrome" | "msedge" | "firefox" | null,
  "applies_to": for block only: "launch" | "play" | "close" | "all",
  "adapter_check": for music limits only, exactly "pre_play: skip if track.explicit" or "pre_play: cap {app} volume at N%", otherwise "",
  "condition": when the rule applies, or null when it always applies: {"kind": "time_after" | "time_before" | "time_between", "start": "HH:MM", "end": "HH:MM"} for clock times; {"kind": "other", "text": "<the condition in the user's words>"} for anything Cygnus cannot check (unsaved work, meetings, who is around),
  "expects": one short sentence describing what the user would see when the rule worked (e.g. "music is playing", "the file is saved", "the search results are on screen"),
  "steps": for steps only, the ordered list of steps (see below)
}
Meaning of type:
- search_site: the command searches a website for something the user names.
- open_site: the command just opens a website.
- steps: the user wants more than one action in a row (e.g. search, choose a result, open it, press play), or actions inside a desktop app.
Steps for a website: {"op": "search"}, {"op": "pick", "count": 5} (show the top results and let the user choose), {"op": "open"}, {"op": "click", "target": "play"} (target = the button or thing to click, in words), {"op": "wait", "seconds": 2}.
Steps for a desktop app: {"op": "launch"}, {"op": "click", "target": "<button name>"}, {"op": "type", "target": "<box name>", "text": "{query}"}, {"op": "key", "keys": "{ENTER}"}.
Keys use this notation: {ENTER} {TAB} {ESC} {UP} {DOWN} {LEFT} {RIGHT} {HOME} {END} {F1}..{F12}, ^ = Ctrl, % = Alt, + = Shift, so Ctrl+S is "^s", Alt+F4 is "%{F4}", Ctrl+Shift+T is "^+t".
For a desktop app, prefer a click target that is one of the app's KNOWN CONTROLS OR ABILITIES listed above, written exactly as listed.
Add "confirm": true to any step the user would want to approve first. After a search, always add a pick step so the user chooses.
- block: the user wants something prevented (never / don't / no). A block with a time in it gets a time condition.
- reminder: a preference or limit Cygnus should keep in mind (volume caps, explicit content, "always use X").
Only use a site the user wrote or clearly named by a well-known name. Never invent one.
If WHAT CYGNUS SHOULD DO is empty, the command phrase holds the whole rule - read it as one.
JSON only. /no_think"""

# Well-known names the user may say instead of a domain. A model site is
# accepted when the user's words name it here (or in web_registry), so
# "open youtube music" no longer asks "Which website?".
_KNOWN_SITES = {
    "youtube music": "music.youtube.com", "yt music": "music.youtube.com",
    "youtube": "youtube.com", "gmail": "mail.google.com",
    "google drive": "drive.google.com", "google docs": "docs.google.com",
    "google maps": "maps.google.com", "google": "google.com",
    "netflix": "netflix.com", "spotify web": "open.spotify.com",
    "facebook": "facebook.com", "messenger": "messenger.com",
    "whatsapp web": "web.whatsapp.com", "whatsapp": "web.whatsapp.com",
    "instagram": "instagram.com", "tiktok": "tiktok.com",
    "twitter": "x.com", "x.com": "x.com", "reddit": "reddit.com",
    "github": "github.com", "chatgpt": "chatgpt.com", "wikipedia": "wikipedia.org",
    "amazon": "amazon.com", "discord": "discord.com", "twitch": "twitch.tv",
    "linkedin": "linkedin.com", "pinterest": "pinterest.com",
    "duckduckgo": "duckduckgo.com", "bing": "bing.com", "outlook": "outlook.live.com",
}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_CONDITION_KINDS = ("time_after", "time_before", "time_between", "other")


# ------------------------------------------------------------------ helpers --

def _slug(text):
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "_".join(words) or "rule"


def _clean_phrase(text):
    t = re.sub(r"\{[^}]*\}", " ", text or "")
    t = re.sub(r"[\"'.!?,;:]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _bare_host(url_or_domain):
    h = re.sub(r"^https?://", "", (url_or_domain or "").strip().lower())
    h = h.split("/", 1)[0].split("?", 1)[0].rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _domains_in(text):
    return [_bare_host(u.rstrip(".,;:!?)")) for u in _URL_RE.findall(text or "")]


def _specific_enough(phrase):
    """A trigger needs a word beyond the verb and browser ('search movies in
    brave', not 'search brave') or it would capture every search."""
    words = [w for w in re.findall(r"[a-z0-9]+", phrase) if w not in _SPECIFIC_STOP]
    rest = [w for w in words if w not in _VERBS and w not in _BROWSER_KEYS]
    return len(words) >= 2 and bool(rest)


def _slot_guess(trigger):
    """'search a movie in brave' -> 'movie name'."""
    for w in re.findall(r"[a-z0-9]+", (trigger or "").lower()):
        if w in _SPECIFIC_STOP or w in _VERBS or w in _BROWSER_KEYS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        return f"{w} name"
    return "name"


def _browser_in(text):
    low = (text or "").lower()
    for word, key in _BROWSER_KEYS.items():
        if re.search(r"\b" + re.escape(word) + r"\b", low):
            return key
    return None


def browser_label(key):
    return _BROWSER_LABELS.get(key or "", key or "your browser")


def split_draft(line):
    """(command phrase, action text) from one draft line.

    'search movies in brave -> search it on hollymoviehd.cc' and
    'when I say search movies, search hollymoviehd.cc' split here; anything
    else is returned whole with an empty action (the model may split it)."""
    line = (line or "").strip()
    m = _WHEN_I_SAY_RE.match(line)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    parts = _ARROW_RE.split(line, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return line, ""


def is_command_phrase(phrase):
    return bool(COMMAND_START_RE.match((phrase or "").strip()))


def _parse_json(raw):
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S | re.I).strip()
    raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw, flags=re.I)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _router_models(config):
    """9router models to ask: the three router_health says answer right now,
    else the brain's model and two of its fallbacks. The configured model has
    been dead for days (503), and asking it first cost every call."""
    try:
        import importlib as _il
        router_health = _il.import_module("cygnus.brain.router_health")   # honours sys.modules (tests swap in a stand-in)
        healthy = [m for m in router_health.healthy_models() if m][:3]
    except Exception:
        healthy = []
    return healthy or [config.ROUTER_MODEL] + list(
        getattr(config, "ROUTER_FALLBACK_MODELS", []) or [])[:2]


def _ask_llm(prompt, local=True, timeout=20, max_tokens=600):
    """(parsed JSON dict, model) from the first model that answers, else
    (None, reason). 9router's answering models first, then Google's
    (one_shot.py), then - unless local=False - the local Ollama model, which
    on this CPU takes ~20 s or more and must not sit on a reply's path."""
    try:
        from openai import OpenAI

        from cygnus import config
        from cygnus.brain import one_shot
    except Exception as e:
        return None, f"no LLM client ({type(e).__name__})"
    messages = [{"role": "user", "content": prompt}]
    try:
        text, model = one_shot.complete(_router_models(config), messages, timeout=timeout,
                                        accept=_parse_json, temperature=0, max_tokens=max_tokens)
        return _parse_json(text), model
    except RuntimeError as e:
        errors = [str(e)]
    if local and config.JARVIS_USE_OLLAMA:
        model = config.OLLAMA_MODEL
        try:
            client = OpenAI(base_url=config.OLLAMA_BASE_URL, api_key=config.OLLAMA_API_KEY or "ollama",
                            timeout=90, max_retries=0)
            r = client.chat.completions.create(model=model, temperature=0, max_tokens=max_tokens,
                                               messages=messages)
            data = _parse_json(r.choices[0].message.content or "")
            if data:
                return data, model
            errors.append(f"{model}: no JSON")
        except Exception as e:
            errors.append(f"{model}: {type(e).__name__}")
    return None, "; ".join(errors)


def _fallback_raw(trigger, action_text):
    """Keyword-only structure for when no model answers. Only used when the
    user's words name a site - otherwise the caller keeps the legacy parser."""
    text = f"{trigger} {action_text}"
    domains = _domains_in(text)
    if not domains:
        return None
    return {
        "type": "search_site" if _SEARCH_RE.search(text) else "open_site",
        "trigger": trigger, "triggers": [], "site": domains[0],
        "search_url": None, "slot": _slot_guess(trigger),
        "browser": _browser_in(text),
    }


# ------------------------------------------------------------------ compile --

def _summary(rule_type, trigger, site, slot, browser, app_key, action_text, applies_to):
    where = browser_label(browser)
    if rule_type == "search_site":
        article = "an" if slot[:1].lower() in "aeiou" else "a"
        return (f'When you say "{trigger}" with {article} {slot}, I\'ll search {site} '
                f"for it in {where}.")
    if rule_type == "open_site":
        return f'When you say "{trigger}", I\'ll open {site} in {where}.'
    if rule_type == "block":
        act = {"launch": "open", "play": "play", "close": "close"}.get(applies_to, "use")
        return f"I won't {act} {app_key} when this applies: {action_text or trigger}."
    return f"I'll keep this in mind for {app_key}: {action_text or trigger}."


def _clean_steps(raw_steps, web):
    """The model's steps, made safe (see _clean_steps_report)."""
    return _clean_steps_report(raw_steps, web)[0]


def _clean_steps_report(raw_steps, web):
    """The model's steps, made safe: allowed ops only, bounded fields, a pick
    after every search (the user chooses), an open before any page click, and
    "confirm" forced on clicks that play, buy, send or delete.

    Returns (steps, dropped): `dropped` says, in plain words, what the model
    asked for that Cygnus cannot do, so the user is told instead of finding a
    silently weaker rule ("press ctrl s" used to vanish)."""
    from cygnus.rules import steps as rules_steps
    allowed = rules_steps.WEB_OPS if web else rules_steps.DESKTOP_OPS
    out, dropped = [], []
    for s in raw_steps if isinstance(raw_steps, list) else []:
        if not isinstance(s, dict):
            continue
        if s.get("op") not in allowed:
            where = "a website" if web else "a desktop app"
            dropped.append(f'the step "{s.get("op")}" (not something I can do in {where})')
            continue
        step = {"op": s["op"]}
        for key, cap in (("target", 60), ("text", 100), ("keys", 30), ("question", 120)):
            if s.get(key):
                step[key] = str(s[key])[:cap]
        try:
            if step["op"] == "pick":
                step["count"] = min(max(int(s.get("count") or 5), 2), 10)
            if step["op"] == "wait":
                step["seconds"] = min(max(float(s.get("seconds") or 1), 0), 10)
        except (TypeError, ValueError):
            step.setdefault("count" if step["op"] == "pick" else "seconds",
                            5 if step["op"] == "pick" else 1)
        if step["op"] == "click" and (s.get("confirm") is True
                                      or _CONSEQUENTIAL_RE.search(step.get("target", ""))):
            step["confirm"] = True
        if step["op"] == "key" and not rules_steps.SAFE_KEYS_RE.match(step.get("keys", "")):
            dropped.append(f'pressing "{step.get("keys", "")}" (only Enter, Tab, arrows, '
                           f'function keys and Ctrl/Alt/Shift letter shortcuts)')
            continue
        out.append(step)
    ops = [s["op"] for s in out]
    if web and "search" in ops and "pick" not in ops:
        out.insert(ops.index("search") + 1, {"op": "pick", "count": 5})
    ops = [s["op"] for s in out]
    if web and "click" in ops and "open" not in ops:
        out.insert(ops.index("click"), {"op": "open"})
    return out[:MAX_STEPS], dropped


def _control_names(entry):
    """What the app is known to have: its abilities and the buttons a window
    scan found (app_abilities stores scanned controls as abilities too)."""
    names = []
    for a in (entry or {}).get("abilities") or []:
        if isinstance(a, dict) and (a.get("name") or a.get("id")):
            names.append(str(a.get("name") or a.get("id")))
    return names


def _target_known(target, names):
    """Does a click target name one of the app's known controls? Every word
    of the target must appear in one control's name (word-boundary)."""
    words = [w for w in re.findall(r"[a-z0-9]+", (target or "").lower()) if len(w) > 1]
    if not words:
        return False
    for name in names:
        low = name.lower()
        if all(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low) for w in words):
            return True
    return False


def _app_context(app_key, entry):
    """The APP CONTEXT block of the prompt: known controls, existing rules."""
    entry = entry if isinstance(entry, dict) else {}
    lines = []
    names = _control_names(entry)
    if names:
        shown = sorted(dict.fromkeys(n[:40] for n in names))[:60]
        lines.append("KNOWN CONTROLS OR ABILITIES of this app (buttons and actions "
                     "Cygnus can already use): " + "; ".join(shown))
    rules = [r for r in entry.get("compiled_rules") or [] if isinstance(r, dict)]
    if rules:
        lines.append("RULES THE USER ALREADY HAS for this app: " + "; ".join(
            f'"{r.get("source_phrase", "")}" -> {r.get("goal") or r.get("summary") or r.get("intent") or ""}'[:120]
            for r in rules[:8]))
    return ("\n" + "\n".join(lines) + "\n") if lines else ""


def _catalog_site(text):
    """The domain of a well-known site the user named in words ("youtube
    music"), from the built-in list or the user's own web registry."""
    low = " " + re.sub(r"[^a-z0-9. ]+", " ", (text or "").lower()) + " "
    best = None
    for name, host in _KNOWN_SITES.items():
        if f" {name} " in low and (best is None or len(name) > len(best[0])):
            best = (name, host)
    if best:
        return best[1]
    try:
        from cygnus.apps import web_registry as wr
        for n in (3, 2, 1):
            words = low.split()
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                if phrase in _SPECIFIC_STOP or phrase in _VERBS:
                    continue
                key = wr.resolve_site(phrase)
                site = wr.get_site(key) if key else None
                if site and site.get("host"):
                    return _bare_host(site["host"])
    except Exception:
        pass
    return None


def _clean_condition(raw):
    """A usable condition, or None. Time conditions are checked at run time;
    anything else is kept as words and reported as a gap by the caller."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").lower()
    if kind not in _CONDITION_KINDS:
        return None
    if kind == "other":
        text = str(raw.get("text") or "").strip()[:120]
        return {"kind": "other", "text": text} if text else None
    start = str(raw.get("start") or raw.get("time") or "").strip()
    end = str(raw.get("end") or "").strip()
    if kind == "time_after" and _TIME_RE.match(start):
        return {"kind": "time_after", "start": start}
    if kind == "time_before" and _TIME_RE.match(end or start):
        return {"kind": "time_before", "end": end or start}
    if kind == "time_between" and _TIME_RE.match(start) and _TIME_RE.match(end):
        return {"kind": "time_between", "start": start, "end": end}
    return None


def describe_condition(cond):
    """'after 23:00', 'before 07:00', 'between 22:00 and 07:00'."""
    if not isinstance(cond, dict):
        return ""
    k = cond.get("kind")
    if k == "time_after":
        return f"after {cond.get('start')}"
    if k == "time_before":
        return f"before {cond.get('end')}"
    if k == "time_between":
        return f"between {cond.get('start')} and {cond.get('end')}"
    if k == "other":
        return cond.get("text") or ""
    return ""


def _steps_summary(trigger, action, app_key):
    from cygnus.rules import sim as rules_sim
    slot = action.get("slot") or ""
    article = "an" if slot[:1].lower() in "aeiou" else "a"
    said = f'When you say "{trigger}"' + (f" with {article} {slot}" if slot else "")
    where = (f"{action['site']} in {browser_label(action.get('browser'))}" if action.get("site")
             else app_key)
    parts, seen_pick = [], False
    for s in action["steps"]:
        try:
            text = rules_sim.describe_step(s, after_pick=seen_pick)
        except TypeError:
            text = rules_sim.describe_step(s)
        parts.append(text.replace("the site", action.get("site") or "the site"))
        seen_pick = seen_pick or s.get("op") == "pick"
    plan = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])}, then {parts[-1]}"
    return f"{said}, I'll {plan} ({where})."


def compile_rule(app_key, trigger, action_text="", feedback="", entry=None, llm=None):
    """Compile one rule. Returns (rule, notes), or (None, notes) when no model
    answered and the text names no site - the caller keeps its legacy path.

    feedback: the user's correction to a previous proposal ('use the site's
    /search/ page'), folded into the prompt so the model revises its answer.
    """
    trigger = (trigger or "").strip()
    action_text = (action_text or "").strip()
    feedback = (feedback or "").strip()
    entry = entry if isinstance(entry, dict) else {}
    notes = []

    is_browser_app = entry.get("category") == "browser"
    prompt = (_PROMPT
              .replace("{app}", app_key)
              .replace("{browser_note}", " It is a web browser." if is_browser_app
                       else " It is a desktop app, not a web browser." if entry.get("bin")
                       else "")
              .replace("{context}", _app_context(app_key, entry))
              .replace("{trigger}", trigger)
              .replace("{action}", action_text)
              .replace("{feedback}",
                       f'\nCORRECTION FROM THE USER: "{feedback}"' if feedback else ""))
    raw, model = (llm or _ask_llm)(prompt)
    compiled_by = f"ai:{model}" if raw else "keywords"
    if not raw:
        notes.append(f"AI compile unavailable ({model}) - compiled from keywords.")
        raw = _fallback_raw(trigger, f"{action_text} {feedback}")
        if raw is None:
            return None, notes

    user_text = f"{trigger} {action_text} {feedback}"
    user_domains = _domains_in(user_text)
    gaps = []

    rule_type = raw.get("type") if raw.get("type") in ACTION_TYPES else None
    site = _bare_host(raw.get("site") or "")
    named = _catalog_site(user_text)
    if site and site not in user_domains and not (
            named and rules_engine.same_site("https://" + site, named)):
        # The model named a site the user never wrote nor named: keep the
        # user's own (a well-known name like "youtube music" counts as named).
        site = named or ""
    if not site and user_domains:
        site = user_domains[0]
    if not site and named and (rule_type in RUNNABLE_TYPES or rule_type is None):
        site = named
    if rule_type is None:
        rule_type = ("search_site" if site and _SEARCH_RE.search(user_text)
                     else "open_site" if site
                     else "block" if _BLOCK_RE.search(user_text) else "reminder")

    clean_trigger = _clean_phrase(raw.get("trigger") or "") or _clean_phrase(trigger)
    # The phrase the user typed stays the primary trigger when they gave one
    # separately; the model's cleanup only replaces a combined draft line.
    if action_text:
        clean_trigger = _clean_phrase(trigger) or clean_trigger

    browser = _BROWSER_KEYS.get(str(raw.get("browser") or "").lower())
    typed_browser = _browser_in(user_text)
    if browser and browser != typed_browser and browser != app_key:
        browser = None
    browser = browser or typed_browser or (app_key if is_browser_app else None)

    steps = []
    web = rule_type != "steps" or bool(site) or is_browser_app
    if rule_type == "steps":
        steps, dropped = _clean_steps_report(raw.get("steps"), web)
        gaps.extend(dropped)
        if not steps:
            rule_type = ("search_site" if site and _SEARCH_RE.search(user_text)
                         else "open_site" if site else "reminder")
            if dropped:
                gaps.append("so this became a reminder, not something I can run")
        elif not web:
            # A desktop click must name a control the app is known to have;
            # otherwise say so now instead of failing when the command is used.
            known = _control_names(entry)
            for s_ in steps:
                if s_.get("op") == "click" and known and not _target_known(s_.get("target"), known):
                    gaps.append(f"I don't know a \"{s_.get('target')}\" button in {app_key} yet; "
                                f"I'll look for it on screen when you use the command")
    has_search = rule_type == "search_site" or any(s["op"] == "search" for s in steps)

    action = {"type": rule_type}
    needs_q = None
    if rule_type in RUNNABLE_TYPES and web:
        if site:
            action.update(site=site, url=f"https://{site}")
        else:
            needs_q = "Which website should I use? Type its address, like example.com."
        action["browser"] = browser
    if steps:
        action["steps"] = steps
    if has_search:
        slot = str(raw.get("slot") or "").strip().lower()[:30] or _slot_guess(clean_trigger)
        search_url = str(raw.get("search_url") or "").strip()
        if not ("{query}" in search_url
                and rules_engine.is_web_url(search_url.replace("{query}", "q"))
                and rules_engine.same_site(search_url, site)):
            search_url = f"https://{site}/?s={{query}}" if site else ""
        # Only an address the user typed is known good; the model's is a guess.
        typed = search_url.split("{query}", 1)[0].lower()
        typed = re.sub(r"^https?://(?:www\.)?", "", typed)
        guessed = bool(search_url) and typed not in user_text.lower()
        if guessed:
            notes.append(f"Search address guessed as {search_url} - press Test to check it.")
        action.update(slot=slot, search_url=search_url, search_url_guessed=guessed)
        sample = str(raw.get("sample") or "").strip()[:60]
        if sample:
            action["sample"] = sample

    applies_to = str(raw.get("applies_to") or "launch").lower()
    if applies_to not in _APPLIES_TO:
        applies_to = "launch"
    adapter_check = str(raw.get("adapter_check") or "").strip()
    if not _MUSIC_CHECK_RE.match(adapter_check):
        adapter_check = ""
    if rule_type == "block":
        action["applies_to"] = applies_to
        enforcement = "hard"
        adapter_check = adapter_check or (_APPLIES_TO[applies_to] + (action_text or trigger)
                                          if _APPLIES_TO[applies_to] else "")
    elif rule_type == "reminder":
        enforcement = "soft"
    else:
        # Runnable rules are neither blocks nor reminders: rules_engine's gate
        # ignores this enforcement, so opening the browser stays silent.
        enforcement = "action"

    condition = _clean_condition(raw.get("condition"))
    if condition and condition.get("kind") == "other":
        gaps.append(f"I can't check \"{condition['text']}\" yet, so the rule applies every time")
        condition = None
    if rule_type == "block" and not condition and re.search(
            r"\b(after|before|until|between|past)\s+\d{1,2}(:\d{2})?\s*(am|pm)?\b", user_text, re.I):
        gaps.append("I couldn't read the time in that, so the rule applies every time")
    goal = str(raw.get("goal") or "").strip()[:200]
    expects = str(raw.get("expects") or "").strip()[:160]

    triggers = []
    for t in [clean_trigger] + [_clean_phrase(str(x)) for x in (raw.get("triggers") or [])
                                if isinstance(x, str)]:
        if t and len(t) <= 80 and t not in triggers and (
                t == clean_trigger or _specific_enough(t)):
            triggers.append(t)
    triggers = triggers[:8]

    summary = (_steps_summary(clean_trigger, action, app_key) if steps
               else _summary(rule_type, clean_trigger, site or "the site",
                             action.get("slot", ""), browser, app_key,
                             action_text, applies_to))
    if condition:
        summary = summary.rstrip(".") + f" (only {describe_condition(condition)})."
    rule = {
        "rule_id": _slug(clean_trigger),
        "source_phrase": clean_trigger,
        "intent": summary,
        "summary": summary,
        "goal": goal or (action_text or trigger),
        "expects": expects,
        "condition": condition,
        "gaps": gaps,
        "action_text": action_text or trigger,
        "enforcement": enforcement,
        "adapter_check": adapter_check,
        "scope": "all_sessions",
        "triggers": triggers,
        "action": action,
        "compiled_by": compiled_by,
        "needs_clarification": bool(needs_q),
        "clarification_question": needs_q,
    }
    return rule, notes


def compile_drafts(app_key, drafts, existing_rules=None, entry=None, llm=None):
    """Compile every draft line of the Apps panel. Returns (rules, notes).

    A line that already has a structured rule is kept as is. A line matching an
    older plain rule is recompiled with that rule's saved action, so the site
    it named is never lost. Lines no model can read keep the legacy keyword
    parser, as before.
    """
    from cygnus.rules import compiler as rc
    by_phrase = {}
    for r in existing_rules or []:
        if isinstance(r, dict) and r.get("source_phrase"):
            by_phrase[_clean_phrase(r["source_phrase"])] = r
    rules, notes = [], []
    for line in drafts or []:
        line = (line or "").strip()
        if not line:
            continue
        trig, act = split_draft(line)
        old = by_phrase.get(_clean_phrase(line)) or by_phrase.get(_clean_phrase(trig))
        if old and isinstance(old.get("action"), dict) and not act:
            rules.append(old)
            continue
        if old and not act:
            saved = old.get("action_text") or old.get("intent") or ""
            if saved and not saved.lower().startswith("unclear"):
                act = saved
        rule, n = compile_rule(app_key, trig, act, entry=entry, llm=llm)
        notes.extend(f"{trig}: {x}" for x in n)
        if rule is None:
            legacy = rc.parse_scaffold(app_key, line)["candidate_rules"]
            answers = {}
            for c in legacy:
                if c.get("needs_clarification"):
                    low = (c.get("source_phrase") or "").lower()
                    quiet = any(m in low for m in ("quiet", "loud", "soft", "low", "calm", "chill"))
                    answers[c["rule_id"]] = (f"under 60% {app_key} volume all sessions" if quiet
                                             else f"{c['source_phrase']}, all sessions")
            rules.extend(rc.apply_clarifications(app_key, legacy, answers))
            continue
        if rule.get("needs_clarification"):
            notes.append(f"{trig}: {rule['clarification_question']}")
        rules.append(rule)
    for r in rules:
        if rc.detect_hard_escalation(r) and r.get("enforcement") not in ("hard", "action"):
            r["enforcement"] = "hard"
    return rules, notes


def propose_markdown(app_key, rules, notes=None):
    """Human-readable proposal for the panel."""
    lines = [f"Proposed rules for {app_key}", ""]
    for r in rules:
        a = r.get("action") or {}
        lines.append(f"- {r.get('summary') or r.get('intent', '')}")
        if a.get("type") in RUNNABLE_TYPES:
            lines.append(f"    type: {a['type']} | browser: {browser_label(a.get('browser'))}")
            if a.get("search_url"):
                lines.append(f"    search address: {a['search_url']}")
            if a.get("steps"):
                from cygnus.rules import sim as rules_sim
                lines.extend(f"    step {i}: {rules_sim.describe_step(s)}"
                             for i, s in enumerate(a["steps"], 1))
            others = [t for t in r.get("triggers") or [] if t != r.get("source_phrase")]
            if others:
                lines.append("    also works for: " + "; ".join(others))
        else:
            lines.append(f"    {r.get('enforcement', 'soft')} | check: "
                         f"{r.get('adapter_check') or '(none)'}")
        if r.get("condition"):
            lines.append(f"    only {describe_condition(r['condition'])}")
        if r.get("expects"):
            lines.append(f"    done when: {r['expects']}")
        for g in r.get("gaps") or []:
            lines.append(f"    can't yet: {g}")
        if r.get("compiled_by"):
            lines.append(f"    compiled by: {r['compiled_by']}")
        v = r.get("verification") or {}
        if v.get("steps"):
            label = {"verified": "VERIFIED", "failed": "FAILED CHECK",
                     "unverified": "NOT VERIFIED", "simulated": "SIMULATED"}.get(
                         v.get("status"), str(v.get("status", "")).upper())
            lines.append(f"    simulation: {label}")
            lines.extend(f"      {s}" for s in v["steps"])
            j = v.get("judge") or {}
            if j:
                lines.append(f"      intent check: {'matches' if j.get('match') else 'MISMATCH'}"
                             f" - {j.get('reason', '')}")
    for n in notes or []:
        lines.append(f"Note: {n}")
    return "\n".join(lines)
