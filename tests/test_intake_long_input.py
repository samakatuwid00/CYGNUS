"""A pasted page resolves without comparing all of it to every name.

resolve_intent ran difflib's ratio() of the whole input against every corpus
entry and an edit distance for every word against every name word, so a
540-word paste took about two seconds (218 ratio() calls, 87k edit distances)
on every HUD turn and in keyword_route: routing gate p99 2787ms, 2026-09-29.
The shortcuts skip only comparisons whose bound says they cannot win, so what
is resolved does not change.
"""

import difflib

import pytest

from cygnus.brain import intake

PARAGRAPH = ("the quarterly report mentions throughput latency and a number of "
             "follow-up items that need owners before the next review ") * 40


def _ratio_calls(text, monkeypatch):
    calls = []
    real = difflib.SequenceMatcher.ratio
    monkeypatch.setattr(difflib.SequenceMatcher, "ratio",
                        lambda self: calls.append(1) or real(self))
    intake.resolve_intent(text, intake.load_corpus())
    return len(calls)


def test_a_long_paste_is_not_compared_to_every_name(monkeypatch):
    assert _ratio_calls(PARAGRAPH, monkeypatch) < 20


def test_an_ask_buried_in_a_long_paste_is_still_found():
    it = intake.resolve_intent(PARAGRAPH + " please search the weather in manila " + PARAGRAPH,
                               intake.load_corpus())
    assert it.verb == "search"


def test_the_whole_text_is_kept_as_what_was_said():
    it = intake.resolve_intent(PARAGRAPH, intake.load_corpus())
    assert it.normalized == intake.normalize(PARAGRAPH)


PAIRS = [("open notepad", "notepad"), ("play taylor son", "taylor swift"),
         ("copy the budget table from excel", "excel"), ("opn notpad", "notepad"),
         (intake.normalize(PARAGRAPH), "spotify"), ("stop", "stop")]


@pytest.mark.parametrize("a,b", PAIRS)
@pytest.mark.parametrize("floor", [0.0, 0.2, 0.249, 0.5, 0.9])
def test_a_floor_only_blurs_scores_that_cannot_beat_it(a, b, floor):
    exact = intake._token_set_ratio(a, b)
    got = intake._token_set_ratio(a, b, floor=floor)
    if exact > floor:
        assert got == exact
    else:
        assert exact <= got <= floor


@pytest.mark.parametrize("a,b", [("notepad", "notpad"), ("son", "swift"),
                                 ("a", "throughput"), ("excel", "excell"),
                                 ("review", "reviewing"), ("x", "x")])
def test_the_length_shortcut_agrees_with_the_edit_distance(a, b):
    # _fuzzy_token's threshold is 0.5 for short words and the cutoff (0.5 here) otherwise.
    score = 1.0 - intake._edit_distance(a, b) / max(len(a), len(b))
    full = 1.0 if a == b else (0.6 if score >= 0.5 else 0.0)
    assert intake._fuzzy_token(a, b, 0.5) == full
