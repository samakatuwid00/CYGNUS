"""consent.py - one reader for the owner's reply to a question Cygnus asked.

Cygnus asks "Confirm?", "Should I remember that ...?", "The app or the
website?", "Which one should I open?". The reply is a yes, a no, a pick among
the options, or not an answer at all: a new request, routed as one. Each
question used to read yes and no with a regex of its own, and they disagreed:
"yeah" answered a preference question but not a delegation confirm
(2026-09-13).
"""

import re
from dataclasses import dataclass

# Politeness that may follow any answer: "yes please", "sige na", "no thanks".
_TAIL = r"(?:[\s,.!]+(?:please|sir|cygnus|jarvis|thanks|thank\s+you|na|po))*[\s,.!]*$"
_YES = r"(?:confirm(?:ed)?|proceed|go\s+ahead|go\s+for\s+it|do\s+it|yes|yeah|yep|yup|sure|oo|opo|sige)"
_MORE_YES = r"(?:[\s,.!]+(?:do\s+it|go\s+ahead|proceed|confirm(?:ed)?|please\s+do))*"
# A yes to a confirm. A bare "ok" is only an acknowledgement, never consent to
# a gated task, so it is not here. Anything after the yes but politeness is a
# command ("yes, open spotify").
YES_RE = re.compile(r"^" + _YES + _MORE_YES + _TAIL, re.I)
# A yes to a light question ("Should I remember that?"), where "ok" is one.
SOFT_YES_RE = re.compile(r"^(?:" + _YES + r"|ok(?:ay)?|alright|please\s+do)" + _MORE_YES + _TAIL, re.I)
# Only decline words may follow, so "no, I meant open spotify" stays a
# correction and "No Time to Die" stays a title. A bare "stop" is left to the
# music and job controls.
NO_RE = re.compile(
    r"^(?:no|nope|nah|cancel|don'?t|do\s+not|never\s*mind|forget\s+it|not\s+now|skip\s+it|"
    r"leave\s+it|hindi|huwag|wag)\b"
    r"(?:[\s,.!]+(?:no|cancel|stop|don'?t|do\s+not|that|it|this|do\s+it|run\s+it|thanks|"
    r"thank\s+you|please|sir|jarvis|cygnus|never\s*mind|forget\s+it))*[\s,.!]*$", re.I)

_ORDINAL = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
            "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "last": -1}
_NUMBER = {"one": 0, "1": 0, "two": 1, "2": 1, "three": 2, "3": 2, "four": 3, "4": 3,
           "five": 4, "5": 4}
# Words that surround a pick without naming one: "no, the website", "I mean Code".
_FILLER = {"no", "nope", "i", "mean", "meant", "actually", "the", "a", "an", "one", "please",
           "sir", "cygnus", "jarvis", "open", "that", "it", "yes", "yeah", "oh", "uh", "um"}
# A pick is short; a longer reply is a new request that happens to share a word.
_PICK_MAX_WORDS = 6


@dataclass
class Reply:
    kind: str                # "yes" | "no" | "pick" | "none"
    index: int | None = None


def _words(text):
    return re.sub(r"[^\w\s]", " ", (text or "").lower()).split()


def _option_words(option):
    """An option is one string ("codium") or the words that name it
    (["app", "application", "program"])."""
    if isinstance(option, str):
        return set(_words(option)) - _FILLER
    return {w.lower() for w in option}


def _index(i, options):
    if i == -1:
        return len(options) - 1
    return i if i < len(options) else None


def pick(text, options):
    """Index of the one option a short reply names ("the second one",
    "number two", "codium", "no, the website"), else None."""
    words = _words(text)
    if not words or not options or len(words) > _PICK_MAX_WORDS:
        return None
    for w in words:
        if w in _ORDINAL:
            return _index(_ORDINAL[w], options)
    if words[-1] in _NUMBER and (len(words) == 1 or (len(words) == 2 and words[0] in ("number", "option"))):
        return _index(_NUMBER[words[-1]], options)
    said = set(words) - _FILLER
    hits = [i for i, option in enumerate(options) if said & _option_words(option)]
    return hits[0] if len(hits) == 1 else None


def read(text, options=None, soft=False, extra_yes=None):
    """The reply to an open question: yes, no, a pick among `options`, or
    none (not an answer: route it as a new request). `soft` also takes "ok"
    as a yes, for questions whose yes risks nothing. `extra_yes` is a pattern
    that says yes to one question in so many words ("make it the default")."""
    t = (text or "").strip()
    if options:
        i = pick(t, options)
        if i is not None:
            return Reply("pick", i)
    if (SOFT_YES_RE if soft else YES_RE).match(t) or (extra_yes is not None and extra_yes.match(t)):
        return Reply("yes")
    if NO_RE.match(t):
        return Reply("no")
    return Reply("none")
