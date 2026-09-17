"""Degenerate input asks instead of routing, and the normalizer sees it first.

classify_intent and _detect_backend never called intake.normalize(), so "",
"   ", ".", "\\x00" all reached _detect_backend, came back "hermes" and were
delegated as a task. Two real gaps in the normalizer are closed here too: ANSI
escapes and Cyrillic look-alikes.
"""

import pytest

import brain_gemini as b
import intake
import routing_eval as re_

DEGENERATE = ["", "   ", "\t\t\t", ".", "?", "!!!!!!!!", "...", "-", ",",
              "\xa0\xa0\xa0", "\x00", "‍‍", "‮", "\x1b[0m"]


@pytest.mark.parametrize("text", DEGENERATE)
def test_nothing_said_is_blank(text):
    assert intake.is_blank(text) is True


@pytest.mark.parametrize("text", ["hello jarvis", "jarvis", "open it", "0"])
def test_something_said_is_not_blank(text):
    assert intake.is_blank(text) is False


@pytest.mark.parametrize("text", DEGENERATE)
def test_a_blank_turn_asks_instead_of_routing(text):
    assert b.classify_intent(text) == b.CLARIFY_ASK
    assert re_.keyword_route(text) == "clarify"


@pytest.mark.parametrize("text", DEGENERATE)
def test_a_blank_turn_answers_in_its_own_words(text):
    """clarify_open's label is shared (routing_eval's clarify vocabulary is
    those three names), but "Open an app or a website?" is the wrong answer to
    someone who said nothing."""
    assert b.fast_path_answer(text)[0] == b.BLANK_ASK


def test_a_bare_open_still_asks_the_app_or_website_question():
    assert b.fast_path_answer("open")[0] == b.OPEN_CLARIFY_Q
    assert b.fast_path_answer("can you open")[0] == b.OPEN_CLARIFY_Q


def test_ansi_escapes_come_off_before_any_rule_reads_the_text():
    assert intake.scrub("\x1b[31mopen chrome\x1b[0m") == "open chrome"
    assert intake.normalize("\x1b[31mopen chrome\x1b[0m") == "open chrome"
    assert intake.scrub("\x1b]0;a title\x07open chrome") == "open chrome"


def test_cyrillic_look_alikes_fold_to_latin():
    assert intake.scrub("ореn chrome") == "open chrome"
    assert intake.normalize("ореn chrome") == "open chrome"


@pytest.mark.parametrize("text", ["Кино", "Привет", "Кино Мулен Руж", "Ελλάδα"])
def test_a_word_written_entirely_in_its_own_script_is_left_alone(text):
    """Folding every token corrupted the names it was meant to protect:
    "play Кино" normalized to "play kиho", and normalize() is what
    resolve_intent fuzzy-matches artists, apps, projects and contacts against,
    so the name no longer matched its own corpus entry. Only a token that
    mixes scripts is an attack."""
    assert intake.scrub(text) == text
    assert text.lower() in intake.normalize(f"play {text}")


def test_a_mixed_script_token_still_folds_beside_an_untouched_one():
    assert intake.scrub("ореn Кино") == "open Кино"


def test_scrub_can_leave_the_line_breaks_alone():
    """The prompt path asks for encoding safety, not reflowing: collapsing
    flattened the system prompt and every prior answer onto one line."""
    block = "line one\nline two\n\n    indented"
    assert intake.scrub(block, collapse=False) == block
    assert intake.scrub(block) == "line one line two indented"
    assert intake.scrub("a\ud800b\x1b[0m\nc", collapse=False) == "ab\nc"


def test_invisible_and_control_characters_come_off():
    assert intake.scrub("open​ chrome\x00") == "open chrome"
    assert intake.scrub("‮open chrome") == "open chrome"
    assert intake.scrub("open\xa0\xa0chrome") == "open chrome"


def test_scrub_keeps_case_words_and_punctuation():
    """It is a sanitizer, not the normalizer: normalize() still does the
    lowercasing and filler stripping on top."""
    assert intake.scrub("Hey Jarvis, open the Notepad?!") == "Hey Jarvis, open the Notepad?!"
    assert intake.normalize("Hey Jarvis, can you please open the Notepad?!") == "open notepad"


def test_a_greeting_is_not_blank_even_though_it_is_all_fillers():
    """normalize() collapses "hello jarvis" to "" because every word is a
    filler. is_blank must not be built on normalize() for that reason."""
    assert intake.normalize("hello jarvis") == ""
    assert not intake.is_blank("hello jarvis")
    assert b.classify_intent("hello jarvis") == "greeting"
    assert re_.keyword_route("hello jarvis") == "greeting"
