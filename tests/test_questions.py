"""Open questions live in dialogue_state and are answered through consent:
"Which one should I open?" takes "the second one", a clarify takes the
option's own words, and a reply that is no answer drops the question."""

import pytest

import dialogue_state
from brain_gemini import answer_choice


@pytest.fixture(autouse=True)
def fresh():
    dialogue_state.reset()
    yield
    dialogue_state.reset()


def test_a_pick_routes_the_chosen_app():
    dialogue_state.ask("pick", "Which one should I open?",
                       {"verb": "open", "options": ["code", "codium", "cursor"]})
    assert answer_choice("the second one") == (None, "open codium")
    assert dialogue_state.pending() is None


def test_a_clarify_routes_the_rewritten_request():
    dialogue_state.ask("clarify", "The Spotify app or the website, sir?",
                       {"labels": [["app", "application"], ["website", "site", "web"]],
                        "options": ["open the spotify app", "open spotify website"]})
    assert answer_choice("no, the website") == (None, "open spotify website")


def test_no_drops_the_question():
    dialogue_state.ask("pick", "Which one should I open?", {"verb": "open", "options": ["code", "codium"]})
    assert answer_choice("no") == ("Okay, sir. I'll leave it.", None)
    assert dialogue_state.pending() is None


def test_a_new_request_is_not_an_answer_and_drops_the_question():
    dialogue_state.ask("pick", "Which one should I open?", {"verb": "open", "options": ["code", "codium"]})
    assert answer_choice("what's the weather tomorrow") == (None, None)
    assert dialogue_state.pending() is None


def test_nothing_open_nothing_answered():
    assert answer_choice("the second one") == (None, None)
    dialogue_state.ask("remember", "Should I remember that?", {"text": "x"})
    assert answer_choice("yes") == (None, None)            # another kind's question stays open
    assert dialogue_state.pending("remember") is not None
