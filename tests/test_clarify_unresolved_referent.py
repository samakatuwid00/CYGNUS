"""An open-shaped request whose target cannot be resolved asks which one.

"open it" used to resolve to nothing, fall through to the desktop verb regex
and open an app called "it". clarify only ever fired on an app/site name
collision; the collision case is asserted here too, so this does not trade one
for the other.
"""

import pytest

from cygnus.brain import core as b
from eval import routing_eval as re_
from cygnus import tools


@pytest.fixture
def empty_registries(monkeypatch):
    """No app and no site resolves, so only the referent rule can answer."""
    from cygnus.apps import web_registry
    monkeypatch.setattr(tools, "_load_app_registry", lambda: {})
    monkeypatch.setattr(tools, "_rule_site_url", lambda name: None)
    monkeypatch.setattr(web_registry, "resolve_site", lambda name: None)
    monkeypatch.setattr(tools, "APP_ALIASES", {})
    return monkeypatch


@pytest.mark.parametrize("target", [
    "it", "that", "this", "them", "those", "the other one", "the same one",
    "that one", "the second one", "the last one", "that thing",
    "the same one as yesterday", "that thing from before", "the usual",
])
def test_an_unresolvable_referent_asks_which_one(target, empty_registries):
    assert tools.resolve_open_target(target) == ("clarify", {"referent": target})


@pytest.mark.parametrize("name", ["notepad", "spotify", "github", "my budget sheet"])
def test_a_real_name_is_not_a_referent(name):
    assert not tools.is_referent(name)


@pytest.mark.parametrize("word", ["next", "previous", "last", "first", "latest"])
def test_a_bare_sequence_word_is_transport_not_a_referent(word):
    """"next" said over a playing track is a skip, not a thing to open. Making
    the "one" optional for this group answered it "Open an app or a website?"."""
    assert not tools.is_referent(word)
    assert b.classify_intent(word) != b.CLARIFY_ASK


@pytest.mark.parametrize("phrase", ["the next one", "the last one", "the first one"])
def test_the_same_word_with_one_still_points_at_something(phrase):
    assert tools.is_referent(phrase)
    assert b.classify_intent(phrase) == b.CLARIFY_ASK


@pytest.mark.parametrize("name", ["project jarvis", "yes sir", "cygnus"])
def test_a_name_ending_in_an_address_is_not_truncated(name):
    """The trailing-vocative strip needs the comma. Without it, any target
    whose last word is "jarvis"/"sir" lost it before the registry lookup."""
    assert tools.strip_target_noise(name) == name


@pytest.mark.parametrize("spoken,target", [
    ("this site, JARVIS", "this site"),
    ("github, please", "github"),
    ("notepad, sir", "notepad"),
])
def test_an_address_after_a_comma_still_comes_off(spoken, target):
    assert tools.strip_target_noise(spoken) == target


def test_the_referent_payload_carries_no_name(empty_registries):
    """action_gate and the Phase 8 fast path tell a referent from a collision
    by the absence of "name" - a collision payload has one."""
    _, found = tools.resolve_open_target("it")
    assert "name" not in found and found["referent"] == "it"


def test_an_open_on_a_referent_routes_to_clarify(empty_registries):
    assert re_.keyword_route("open it") == "clarify"
    assert re_.keyword_route("open the same one as yesterday") == "clarify"
    assert re_.keyword_route("jarvis, can you open that one") == "clarify"


def test_a_bare_referent_and_a_do_it_again_route_to_clarify():
    assert b.classify_intent("the other one") == b.CLARIFY_ASK
    assert b.classify_intent("that thing from before") == b.CLARIFY_ASK
    assert b.classify_intent("do it again") == b.CLARIFY_ASK


def test_close_and_play_on_a_referent_ask_instead_of_acting():
    assert b.classify_intent("close that") == b.CLARIFY_ASK
    assert b.classify_intent("play that one") == "clarify_play"
    assert re_.keyword_route("close that") == "clarify"
    assert re_.keyword_route("play that one") == "clarify"


def test_the_transport_continuations_still_play():
    """gold labels "play it" and "pause it" as music transport, not a target
    to ask about - the referent rule must not swallow them."""
    assert b.classify_intent("play it") == "music"
    assert b.classify_intent("play it now") == "music"
    assert b.classify_intent("pause it") == "stop"
    assert b.classify_intent("resume it") == "music"
    assert b.classify_intent("Play next song, next song, next song.") == "music"
    # "again" is the most ordinary way to ask for a replay. The referent rule
    # accepts a trailing "again", so the transport rule has to as well or the
    # two disagree and a replay is answered "Which song, sir?".
    assert b.classify_intent("play it again") == "music"
    assert b.classify_intent("resume it again") == "music"


def test_a_named_target_still_opens(monkeypatch):
    monkeypatch.setattr(tools, "resolve_open_target",
                        lambda text, force=None, browser=None:
                        {"notepad": ("app", "notepad"), "youtube": ("site", "youtube")}
                        .get(text, (None, text)))
    assert re_.keyword_route("open notepad") == "open_app"
    assert re_.keyword_route("open youtube") == "open_site"


def test_a_named_app_still_closes():
    assert b.classify_intent("close spotify") == "close_app"
    assert b.classify_intent("close the notepad") == "close_app"


def test_the_name_collision_clarify_still_asks(monkeypatch):
    """"open cursor" is both an installed app and a registered site: that
    question is a different one and still carries the name."""
    from cygnus.apps import web_registry
    monkeypatch.setattr(tools, "_load_app_registry", lambda: {"cursor": {}})
    monkeypatch.setattr(tools, "_rule_site_url", lambda name: None)
    monkeypatch.setattr(web_registry, "resolve_site", lambda name: "cursor" if name == "cursor" else None)
    monkeypatch.setattr(web_registry, "get_site", lambda key: {"aliases": []})
    kind, found = tools.resolve_open_target("cursor")
    assert kind == "clarify" and found == {"name": "cursor", "site": "cursor"}


def test_a_referent_is_not_a_link_in_an_open_chain(empty_registries):
    """The chain splitter opens every name in "open vscode, notepad and
    chrome". A pronoun is not one of them."""
    assert b._is_open_target("it") is False


def test_action_gate_does_not_read_a_referent_as_a_collision(empty_registries):
    from cygnus.brain import action_gate
    assert action_gate._app_and_site("it") is None
