"""consent: one reading of yes, no and picks for every question Cygnus asks."""

import pytest

from cygnus.brain import consent

APPS = ["code", "codium", "cursor"]
APP_OR_SITE = [["app", "application", "program"], ["website", "site", "web"]]


@pytest.mark.parametrize("text, kind", [
    ("confirm", "yes"), ("yeah", "yes"), ("sure, go ahead", "yes"), ("sige na", "yes"),
    ("yes please", "yes"), ("oo", "yes"),
    ("ok", "none"), ("yes, open spotify", "none"), ("sure thing", "none"),
    ("no", "no"), ("no thanks", "no"), ("nope, don't do it", "no"), ("hindi", "no"),
    ("not now", "no"), ("no, I meant open spotify", "none"), ("No Time to Die", "none")])
def test_a_confirm_is_read_strictly(text, kind):
    assert consent.read(text).kind == kind


def test_ok_is_a_yes_only_to_a_light_question():
    assert consent.read("ok").kind == "none"
    assert consent.read("okay", soft=True).kind == "yes"
    assert consent.read("alright, thanks", soft=True).kind == "yes"


@pytest.mark.parametrize("text, options, index", [
    ("the second one", APPS, 1), ("number two", APPS, 1), ("codium", APPS, 1),
    ("the last one", APPS, 2), ("2", APPS, 1), ("first", APPS, 0),
    ("the website", APP_OR_SITE, 1), ("no, the app", APP_OR_SITE, 0),
    ("I mean the site please", APP_OR_SITE, 1),
    ("the fourth one", APPS, None),                                   # there is no fourth
    ("open youtube music now please", APPS, None),                   # names none of them
    ("tell me the weather for tomorrow in naga city", APPS, None)])  # too long to be a pick
def test_picks(text, options, index):
    assert consent.pick(text, options) == index


def test_a_word_two_options_share_picks_neither():
    assert consent.pick("code", ["code", "vs code"]) is None


def test_read_prefers_a_pick_then_yes_then_no():
    assert consent.read("the second one", options=APPS) == consent.Reply("pick", 1)
    assert consent.read("no, the website", options=APP_OR_SITE) == consent.Reply("pick", 1)
    assert consent.read("no", options=APPS).kind == "no"
    assert consent.read("yes", options=APPS).kind == "yes"
    assert consent.read("play some jazz", options=APPS).kind == "none"


def test_a_question_can_add_its_own_yes():
    import re
    make_default = re.compile(r"^make it (?:the )?default[\s.!]*$", re.I)
    assert consent.read("make it the default", soft=True, extra_yes=make_default).kind == "yes"
    assert consent.read("make it the default").kind == "none"
