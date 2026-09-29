"""Cygnus switches its own window by voice: the Panel, the Full HUD, the Mini
orb, and minimize (window_view.py). The tray and the top bar buttons already
did it; the owner asked to be able to say it (2026-09-24)."""

import pytest

from cygnus.brain import core as brain_core
from cygnus.brain import window_view
from cygnus.brain.intents import classify_intent, requested_view


@pytest.mark.parametrize("say, view", [
    ("go full HUD", "expanded"),
    ("Switch to the full HUD view.", "expanded"),
    ("Full HD.", "expanded"),                     # how Whisper hears "full HUD"
    ("Cygnus, full screen mode please", None),    # a video's full screen, maybe: not ours
    ("Full HUD yourself.", "expanded"),           # said 2026-09-24, got "I cannot"
    ("go full screen", "fullscreen"),
    ("I mean full screen yourself.", "fullscreen"),  # said 2026-09-24, got "I cannot"
    ("full screen yourself", "fullscreen"),
    ("Fullscreen yourself.", "fullscreen"),
    ("make yourself full screen", "fullscreen"),
    ("Cygnus, go fullscreen please", "fullscreen"),
    ("put yourself in full screen mode", "fullscreen"),
    ("exit full screen yourself", "expanded"),
    ("leave your full screen", "expanded"),
    ("expand yourself", "expanded"),
    ("maximize yourself", "expanded"),
    ("mini orb", "mini"),
    ("hey, go mini", "mini"),
    ("switch to the orb", "mini"),
    ("shrink yourself", "mini"),
    ("Cygnus, mini orb mode.", "mini"),
    ("back to the panel", "sticky"),
    ("go back to the panel please", "sticky"),
    ("open the panel", "sticky"),
    ("panel view", "sticky"),
    ("show yourself", "sticky"),
    ("minimize yourself", "minimize"),
    ("Cygnus, minimise yourself.", "minimize"),
    ("can you hide yourself", "minimize"),
    ("go to the tray", "minimize"),
    ("minimize", "minimize"),
])
def test_what_each_command_asks_for(say, view):
    assert requested_view(say) == view
    assert (classify_intent(say) == "view") == (view is not None)


@pytest.mark.parametrize("say", [
    "minimize it",                               # the window just talked about
    "minimize chrome",
    "open the apps panel",                       # a panel in the HUD, not a view
    "show the running panel",
    "what is the mini orb?",                     # a question, for the model
    "CYGNUS should have the capabilities to panel, mini orb, full hud, and minimize itself",
    "delegate this to claude so you can full hd, panel, mini orb, and minimize yourself",
    "make youtube full screen",
    "exit full screen",                          # the video's, as likely as ours
    "full screen",
])
def test_not_a_view_command(say):
    assert requested_view(say) is None
    assert classify_intent(say) != "view"


def test_minimize_it_stays_an_app_window_command():
    assert classify_intent("minimize it") == "app_reference"


def test_the_turn_answers_at_once_and_names_the_view(monkeypatch):
    from cygnus.brain import agent_loop
    monkeypatch.setattr(agent_loop, "MODE", "off")
    monkeypatch.setattr(window_view, "desktop_open", lambda: True)
    b = brain_core.JarvisBrain()
    assert b.think("go full HUD") == "Going full HUD, sir."
    assert b.last_stats == {"backend": "instant", "intent": "view", "view": "expanded"}
    b.think("minimize yourself")
    assert b.last_stats["view"] == "minimize"


def test_without_the_desktop_app_it_says_so(monkeypatch):
    monkeypatch.setattr(window_view, "desktop_open", lambda: False)
    reply = window_view.answer("mini")
    assert "desktop app" in reply and "isn't open" in reply
