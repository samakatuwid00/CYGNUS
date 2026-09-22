"""action_gate.py - act, confirm or clarify: one decision before Cygnus acts.

Phases 3-4 of CONFIRM_CLARIFY_PLAN.md. For a request about to act, decide()
weighs how risky the action is against how sure Cygnus is of what was meant:

                 sure       unsure
    safe         act        act, unless there are several real choices
    risky        confirm    ask which of the choices was meant, then confirm

"Confirm" is the gates that already exist (the confirm latches in tools);
this module only names it. "Clarify" asks which of the real choices was
meant: an app and a website with the same name, or a name heard too badly
to trust ("play talor swif").

JARVIS_GATE=shadow (the default) only logs what it would do to
logs/gate_shadow.jsonl, next to what actually ran; "lead" asks; "off" skips.
"""

import json
import os
import time
from dataclasses import dataclass, field
import paths

MODE = os.getenv("JARVIS_GATE", "shadow").strip().lower()
SHADOW_LOG = paths.log("gate_shadow.jsonl")
# Unsure, but with no choices to offer: logged as reasons only. The intent
# router leads from 0.85; the semantic router from 0.7.
ROUTER_UNSURE = (0.5, 0.85)
SEMANTIC_UNSURE = (0.6, 0.7)
MAX_OPTIONS = 3

_APP_WORDS = ["app", "application", "program", "desktop"]
_SITE_WORDS = ["website", "site", "web", "browser", "online"]


@dataclass
class Decision:
    kind: str                                     # "act" | "confirm" | "clarify"
    risky: bool = False
    reasons: list = field(default_factory=list)   # why it is unsure, if it is
    question: str = ""
    labels: list = field(default_factory=list)    # the words that pick each option
    options: list = field(default_factory=list)   # the request each option routes as


def is_risky(text):
    """The request changes the machine, by the lists the confirm gates use."""
    import tools
    t = text or ""
    return bool(tools._MUTATING_RE.search(t) or tools._DESTRUCTIVE_RE.search(t)
                or tools._CHANGE_RE.search(t))


def _app_and_site(target):
    """The name both an installed app and a website answer to, or None."""
    import tools
    kind, found = tools.resolve_open_target(target)
    if kind != "clarify":
        return None
    if isinstance(found, dict):
        # A referent payload ("open it") carries no name: nothing answers to
        # two things here, there is simply nothing named at all.
        return found.get("name") or (None if found.get("referent") else target)
    return target


_corpus = None


def _intake(text):
    """intake's reading of the request (a fuzzy match against known names)."""
    global _corpus
    import intake
    if _corpus is None:
        _corpus = intake.load_corpus()
    return intake.resolve_intent(text, _corpus)


def _weak_match(text):
    """(question, labels, options) when intake heard a name it cannot trust
    and has two or more candidates for it, else None."""
    found = _intake(text)
    if not (found.entity and found.needs_confirmation()):
        return None
    verb = (found.verb or "").lower()
    cands = [c for c in found.candidates if c and c.lower() != verb][:MAX_OPTIONS]
    if len(cands) < 2:
        return None
    options = [f"{verb} {c}" if verb and not c.lower().startswith(verb) else c for c in cands]
    said = ", ".join(cands[:-1]) + " or " + cands[-1]
    return f"Did you mean {said}, sir?", cands, options


def decide(text, open_target=None, semantic_score=None, router_confidence=None):
    """The one decision for a request about to act (see the table above)."""
    risky = is_risky(text)
    reasons, question, labels, options = [], "", [], []
    if open_target:
        name = _app_and_site(open_target)
        if name:
            reasons.append("an app and a website share the name")
            question = f"The {name} app or the website, sir?"
            labels = [_APP_WORDS, _SITE_WORDS]
            options = [f"open the {name} app", f"open {name} website"]
    if not options:
        weak = _weak_match(text)
        if weak:
            reasons.append("a name heard too badly to trust")
            question, labels, options = weak
    if semantic_score is not None and SEMANTIC_UNSURE[0] <= semantic_score < SEMANTIC_UNSURE[1]:
        reasons.append(f"semantic pick unsure ({semantic_score:.2f})")
    if router_confidence is not None and ROUTER_UNSURE[0] <= router_confidence < ROUTER_UNSURE[1]:
        reasons.append(f"router unsure ({router_confidence:.2f})")
    kind = "clarify" if len(options) >= 2 else "confirm" if risky else "act"
    return Decision(kind, risky, reasons, question, labels, options)


def ask(decision):
    """Put the clarify question to the owner; brain.answer_choice reads the reply."""
    import dialogue_state
    dialogue_state.ask("clarify", decision.question,
                       {"labels": decision.labels, "options": decision.options}, owner="gate")
    return decision.question


def log_shadow(text, decision, ran):
    """What the gate would have done, next to what the turn actually did."""
    rec = {"ts": round(time.time(), 1), "mode": MODE, "text": (text or "")[:200],
           "would": decision.kind, "risky": decision.risky, "reasons": decision.reasons,
           "options": decision.options, "ran": ran}
    try:
        os.makedirs(os.path.dirname(SHADOW_LOG), exist_ok=True)
        with open(SHADOW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
