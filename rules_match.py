"""rules_match.py - the semantic tier for the user's rules.

rules_engine.match_action is the fast tier: keyword triggers, in order, no
model call. It misses "it's gym time" for a "gym time" rule and "sum up this
column" for "total it". On every miss (owner's decision, 2026-09-15) this
module asks one short model question over the rule summaries - all enabled
apps, the app in front first - plus what is on screen, and gets back the rule
the user meant, what they filled in, and how sure the model is.

    semantic(utterance) -> {"rule", "owner", "action", "slot", "confidence",
                            "entry", "trigger"} | None

Sure (>= SURE) runs; unsure (>= ASK) asks "Did you mean your <rule> rule?";
below that, nothing. A yes runs the rule and learns the phrase as a trigger,
so the fast tier catches it next time.

Budget: SEMANTIC_TIMEOUT seconds on the router only (never the local Ollama,
which takes 20 s+ on this CPU). A dead router costs one failed connect.
"""

import json
import re
import time
from pathlib import Path

import rules_engine

SURE = 0.75
ASK = 0.4
SEMANTIC_TIMEOUT = 5.0
MAX_RULES = 40
_MIN_WORDS = 2

_PROMPT = """The user of a voice assistant said: "{utterance}"
{context}
These are the user's own rules (commands they taught the assistant). Pick the ONE rule the user meant, if any.
{rules}

Rules of judgement:
- Match by meaning, not wording: "it's gym time" means the "gym time" rule; "sum up this column" means "total it".
- A rule from the app in front, or the app the user names, wins over another app's.
- Words the user added that are not part of the command are what they filled in (the "fills" part): "message jd I'm late" fills "I'm late".
- If the user is asking a question, chatting, or asking for something no rule does, answer null.

Answer ONLY JSON: {{"rule": "<app>/<rule_id>" or null, "slot": "<what they filled in, or empty>", "confidence": 0.0 to 1.0}}
/no_think"""

# "I'm on my way" is not a command; guard the model's cost on such turns
# is small, but a yes/no or one word is never a rule.
_SKIP_RE = re.compile(r"^\s*(?:yes|no|yeah|nope|ok|okay|thanks|thank you|hi|hello|hey)\b[\s.!?]*$", re.I)


# ------------------------------------------------------------- catalogue --

def _apps():
    try:
        return rules_engine._load_json(rules_engine.APP_REGISTRY_PATH).get("apps") or {}
    except Exception:
        return {}


def catalogue(apps=None, front=None):
    """[(owner, rule, action)] of runnable rules, app in front first."""
    apps = _apps() if apps is None else apps
    out = []
    for owner, entry in apps.items():
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        for rule in entry.get("compiled_rules") or []:
            action = rules_engine.action_of(rule, owner, entry)
            if action:
                out.append((owner, rule, action, entry))
    out.sort(key=lambda t: (t[0] != front, t[0]))
    return out[:MAX_RULES]


def _line(owner, rule, action):
    trig = rule.get("source_phrase") or rule.get("rule_id") or ""
    others = [t for t in rule.get("triggers") or [] if t and t != trig][:3]
    goal = rule.get("goal") or rule.get("summary") or rule.get("intent") or ""
    fills = action.get("slot") or ("" if action.get("type") != "search_site" else "name")
    bits = [f'- {owner}/{rule.get("rule_id")}: say "{trig}"']
    if others:
        bits.append("(or " + "; ".join(f'"{o}"' for o in others) + ")")
    if fills:
        bits.append(f"fills: {fills}")
    bits.append(f"-> {goal[:110]}")
    return " ".join(bits)


def _context():
    try:
        import dialogue_state
        snap = dialogue_state.snapshot()
    except Exception:
        return ""
    lines = []
    if snap.get("active_app"):
        lines.append(f"App in front: {snap['active_app']}.")
    last = snap.get("last_result") or {}
    if last:
        what = last.get("title") or last.get("url") or last.get("app") or ""
        if what:
            lines.append(f"On screen from the last command: {what}.")
    return " ".join(lines)


# ---------------------------------------------------------------- ask --

def semantic(utterance, apps=None, llm=None, front=None):
    """The rule the user meant, by meaning. None when there are no rules, the
    utterance is too short, the router is silent, or nothing fits."""
    text = (utterance or "").strip()
    if len(text.split()) < _MIN_WORDS or _SKIP_RE.match(text):
        return None
    if front is None:
        try:
            import dialogue_state
            front = dialogue_state.active_app()
        except Exception:
            front = None
    cat = catalogue(apps, front)
    if not cat:
        return None
    prompt = _PROMPT.format(utterance=text.replace('"', "'"), context=_context(),
                            rules="\n".join(_line(o, r, a) for o, r, a, _ in cat))
    t0 = time.time()
    if llm is None:
        import rules_ai
        raw, model = rules_ai._ask_llm(prompt, local=False, timeout=SEMANTIC_TIMEOUT,
                                       max_tokens=120)
    else:
        raw, model = llm(prompt)
    if not raw or not raw.get("rule"):
        return None
    key = str(raw.get("rule") or "")
    try:
        conf = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    owner, _, rid = key.partition("/")
    other = _names_other_app(text, owner)
    if other:
        # "play lo-fi on spotify" was offered the "play music brave" rule
        # (2026-09-22): the user named the app, and it is not this rule's.
        print(f"[rules] semantic guess {key} dropped: the words name {other}", flush=True)
        return None
    for o, rule, action, entry in cat:
        if o == owner and rule.get("rule_id") == rid:
            slot = str(raw.get("slot") or "").strip(" .!?,")[:120]
            _audit(text, key, conf, slot, model, time.time() - t0)
            return {"rule": rule, "owner": o, "action": action, "slot": slot,
                    "confidence": conf, "entry": entry, "trigger": None}
    return None


def _known_apps():
    """The owner's active apps (registered and enabled), by key."""
    try:
        import app_abilities
        import curate
        return {k for k in app_abilities.load_apps().get("apps", {}) if curate.is_registered(k)}
    except Exception:
        return set()


def _names_other_app(text, owner):
    """An active app the words name that is not `owner`, else None."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    others = sorted(k for k in _known_apps() if len(k) >= 4 and k in words and k != (owner or "").lower())
    return others[0] if others else None


def _audit(text, key, conf, slot, model, took):
    try:
        import audit
        audit.log_call("rules.semantic", {"said": text[:120], "rule": key, "confidence": conf,
                                          "slot": slot, "model": model}, took, "matched")
    except Exception:
        pass


# -------------------------------------------------------------- learn --

def learn_trigger(owner, rule_id, phrase, registry_path=None):
    """Remember the user's own wording as another trigger of the rule, so the
    fast tier catches it next time. The panel shows it under "also works
    for"; the user can clear it with the rule. Returns True when written."""
    import rules_ai
    clean = rules_ai._clean_phrase(phrase)
    if not clean or len(clean) > 80:
        return False
    try:
        import rules_compiler as rc
        path = Path(registry_path or rules_engine.APP_REGISTRY_PATH)
        data = rc._load_registry(path)
        entry = (data.get("apps") or {}).get(owner) or {}
        for rule in entry.get("compiled_rules") or []:
            if isinstance(rule, dict) and rule.get("rule_id") == rule_id:
                trigs = [t for t in rule.get("triggers") or [] if isinstance(t, str)]
                if clean in trigs or clean == rule.get("source_phrase"):
                    return False
                rule["triggers"] = (trigs + [clean])[:12]
                rc._write_registry(path, data)
                return True
    except Exception:
        return False
    return False


def describe(match):
    """'your "gym time" rule (spotify)' for the did-you-mean question."""
    rule = match["rule"]
    return f'your "{rule.get("source_phrase") or rule.get("rule_id")}" rule ({match["owner"]})'
