"""What a delegated agent is told to do, and how faithfully.

Before 2026-09-24 a coding agent (Claude Code, Codex, Gemini, Hermes) got the
owner's words as heard by speech recognition, or the router model's own
rephrasing of them, buried under a page of profile, session and vault text.
Misheard words went through unfixed, "fix the thing we talked about" arrived
as a pronoun, and a constraint the router dropped ("don't touch the tests")
was gone for good.

Three helpers fix that, each usable on its own:

- restate(): one small model call that turns the spoken request into a task
  brief (goal, scope, done-when, constraints, open questions). Keeps every
  constraint; never adds goals. With no model up, falls back to the words as
  spoken, minus lead-in fillers.
- dropped_constraints(): the negations, file names, numbers and quoted
  strings in the owner's words that a rephrasing lost. The agent loop uses
  it to distrust the router's version of the task.
- compose(): the prompt itself, task first, the owner's own words next, and
  the background (profile, session, vault) last and marked as possibly
  irrelevant. Agents weight the start and the end of a prompt; the task used
  to sit under 2-4 KB of context.
"""

from __future__ import annotations

import json
import re

RESTATE = True              # tests and offline runs switch the model call off
# Off the turn (the job's own thread), so a thinking model may take its time.
# Gemini 3.1 Pro spent a 500-token budget thinking and returned half a JSON
# object (2026-09-24); the answer itself is under 300 tokens.
RESTATE_TIMEOUT_S = 40
RESTATE_MAX_TOKENS = 2000

_RESTATE_PROMPT = """You turn a spoken request into a task brief for a coding agent that has no other context.

Owner's words, from speech recognition (words may be misheard):
"{raw}"
{draft}
Rules:
- Keep every constraint, name, file, path, number and negation from the owner's words. Drop none.
- Fix a misheard word only when the fix is certain from the rest of the sentence.
- Resolve "it", "that", "this" only when the words themselves say what it is; otherwise keep the word and list it under "unclear".
- Never add goals, files or steps the owner did not ask for.
- Keep the owner's language.

Answer with JSON only:
{{"task": "<one to three sentences: what to do, where, and how the agent knows it is done>",
 "constraints": ["<each must or must-not, in the owner's words>"],
 "unclear": ["<what the agent should ask about before guessing, or nothing>"]}}"""


def _ask(prompt: str):
    """(parsed JSON dict, model) or (None, reason). Cloud models only: the
    local CPU model takes 20 s or more and this sits on a dispatch."""
    from cygnus.rules import ai as rules_ai
    return rules_ai._ask_llm(prompt, local=False, timeout=RESTATE_TIMEOUT_S,
                             max_tokens=RESTATE_MAX_TOKENS)


def _plain(raw: str) -> str:
    """The words as spoken, minus 'okay jarvis' lead-ins and 'no, I meant'."""
    from cygnus.tools.phrases import strip_correction_prefix, strip_fillers
    return strip_fillers(strip_correction_prefix((raw or "").strip())) or (raw or "").strip()


def _lines(items) -> list[str]:
    out = []
    for it in items if isinstance(items, (list, tuple)) else []:
        s = str(it or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def restate(task: str, raw: str | None = None, ask=None) -> str:
    """The task as a brief the agent can act on.

    `task` is what the caller wants run (the router's rephrasing, or the words
    with the "delegate this to Claude" part removed); `raw` is what the owner
    said. Both go to the model. The result is the model's task, then its
    constraints and open questions as lists. When no model answers, or it
    drops a constraint the owner's words carry, the answer is the words as
    spoken: a worse brief is still better than a wrong one.
    """
    task = (task or "").strip()
    raw = (raw or "").strip() or task
    fallback = _plain(task) if task else _plain(raw)
    if not RESTATE or not fallback:
        return fallback
    draft = ""
    if task and task != raw:
        draft = f'\nThe assistant\'s draft of the task (may have dropped details):\n"{task}"\n'
    try:
        data, _model = (ask or _ask)(_RESTATE_PROMPT.format(raw=raw, draft=draft))
    except Exception:
        data = None
    if not isinstance(data, dict):
        return fallback
    text = str(data.get("task") or "").strip()
    if len(text) < 8:
        return fallback
    constraints = _lines(data.get("constraints"))
    unclear = _lines(data.get("unclear"))
    body = text
    if constraints:
        body += "\nConstraints:\n" + "\n".join(f"- {c}" for c in constraints)
    if unclear:
        body += "\nUnclear (ask before guessing):\n" + "\n".join(f"- {u}" for u in unclear)
    if dropped_constraints(raw, body):
        return fallback
    return body


# ------------------------------------------------------------ constraints --

_STOP = {"the", "a", "an", "any", "it", "this", "that", "these", "those", "to", "of", "in",
         "on", "at", "my", "your", "our", "its", "and", "or", "with", "for", "me", "please"}
# "don't touch the tests", "without changing the API", "never delete", "keep the
# phone features", "only in the sidebar". "not" alone is left out: "not quite
# aligned" is a description, and a rephrase that says "misaligned" is right.
_NEGATION = re.compile(
    r"\b(?P<neg>don'?t|do\s+not|never|without|avoid|no|keep|only|leave)\s+"
    r"(?P<what>(?:[\w'./\\-]+\s*){1,4})", re.I)
_NEG_MARK = re.compile(r"\b(?:don'?t|do\s+not|never|without|avoid|no|not|must\s+not)\b", re.I)
_FILE = re.compile(
    r"\b[\w-]+(?:[./\\][\w-]+)*\.(?:py|js|ts|tsx|jsx|html|css|md|json|txt|yml|yaml|toml|"
    r"ps1|sh|csv|sql|env|ini|cfg|xml|svg|png|jpg)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.:]\d+)?(?![\w.])")
_QUOTED = re.compile(r"[\"“]([^\"”]{2,80})[\"”]")


def _has_word(text: str, word: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.I) is not None


def dropped_constraints(raw: str, task: str) -> list[str]:
    """What the owner's words require that `task` no longer says.

    Negations and keep/only phrases (their content words, plus some negation
    in the task for the negated ones), file names, numbers and quoted strings.
    Each is checked as a word, case-insensitively. An empty list means the
    rephrasing kept everything this can see, not that it is faithful.
    """
    raw = raw or ""
    task = task or ""
    if not raw.strip() or not task.strip():
        return []
    lost: list[str] = []
    for m in _NEGATION.finditer(raw):
        neg = m.group("neg").lower()
        words = [w.strip(" .,;:!?'\"") for w in m.group("what").split()]
        words = [w for w in words if w and w.lower() not in _STOP and len(w) > 2]
        if not words:
            continue
        # The object is what matters: "don't touch the tests" restated as "do
        # not modify the tests" kept the constraint, so only the last content
        # word ("tests") has to survive, with a negation for the negated ones.
        kept = _has_word(task, words[-1])
        if kept and neg not in ("keep", "only", "leave") and not _NEG_MARK.search(task):
            kept = False
        if not kept:
            lost.append(m.group(0).strip())
    for pat in (_FILE, _QUOTED, _NUMBER):
        for m in pat.finditer(raw):
            piece = m.group(1) if pat is _QUOTED else m.group(0)
            if piece and not _has_word(task, piece):
                lost.append(piece)
    return list(dict.fromkeys(lost))


# ---------------------------------------------------------------- compose --

BACKGROUND_HEAD = ("BACKGROUND (about the owner and this machine; use what helps, "
                   "ignore what does not fit the task):")


def compose(task: str, raw: str | None, instructions: str, background: str) -> str:
    """The prompt: instructions, TASK, the words as spoken, then background."""
    task = (task or "").strip()
    raw = (raw or "").strip()
    out = (instructions.rstrip() + "\n\n") if instructions and instructions.strip() else ""
    out += f"TASK:\n{task}\n"
    if raw and raw != task:
        out += f"\nAS SPOKEN (speech recognition; may be misheard):\n{raw}\n"
    if background and background.strip():
        out += f"\n{BACKGROUND_HEAD}\n{background.strip()}\n"
    return out


def audit_json(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)[:600]
    except Exception:
        return str(obj)[:600]
