"""The computer's own volume: spoken commands reach set_volume, and set_volume
drives Core Audio (or the volume keys where Core Audio can't be reached).

"Turn the volume up to 100%" had no route on 2026-09-24: the model told the
owner to use the keyboard keys, and the next try went to Claude Code as a
coding task."""

import pytest

from cygnus.apps import volume
from cygnus.brain import core as b
from cygnus.brain.intents import parse_volume


@pytest.mark.parametrize("text, action, percent", [
    ("Turn the volume up up to 100%.", "set", 100),
    ("turn the volume up to 100 percent", "set", 100),
    ("set the volume to 40", "set", 40),
    ("volume 30%", "set", 30),
    ("lower the volume to twenty", "set", 20),
    ("make it 100% volume", "set", 100),
    ("turn the volume all the way up", "set", 100),
    ("max volume", "set", 100),
    ("volume at half", "set", 50),
    ("turn the volume up", "up", None),
    ("can you turn up the volume", "up", None),
    ("Cygnus, turn the sound down", "down", None),
    ("volume down by 20", "down", 20),
    ("turn the volume up 20 percent", "up", 20),
    ("turn the volume up a little", "up", volume.SMALL_STEP),
    ("louder", "up", None),
    ("a bit quieter please", "down", volume.SMALL_STEP),
    ("make it louder", "up", None),
    ("turn it down", "down", None),
    ("turn the music down", "down", None),
    ("mute", "mute", None),
    ("mute the volume", "mute", None),
    ("unmute the sound", "unmute", None),
    ("what's the volume", "get", None),
    ("what is the volume at?", "get", None),
])
def test_a_spoken_volume_command_is_read(text, action, percent):
    assert parse_volume(text) == {"action": action, "percent": percent}
    assert b.classify_intent(text) == "volume"


@pytest.mark.parametrize("text", [
    # The owner asking for the feature to be built is a hand-off, not a volume change.
    "I mean, delegate the Claude code that you should have the ability to turn up "
    "and down the volume of the computer.",
    "turn the spotify volume down",            # the app's own ability
    "close discord, mute the volume, then open my email",   # a chain
    "play music, turn the volume up, and tell me the time",
    "open sound settings",
    "pull up the sound settings",
    "keep the volume under 40%",                # a standing rule, not a level
    "how do I turn the volume up",
    "why is the sound so low",
    "turn off the music",                       # a stop
    "speak louder",                             # Cygnus's own voice
    "put up some music",
    "mute discord",
])
def test_what_is_not_a_volume_command_is_left_alone(text):
    assert parse_volume(text) is None
    assert b.classify_intent(text) != "volume"


def test_the_stop_and_chain_routes_keep_their_phrases():
    assert b.classify_intent("turn off the music") == "stop"
    assert b._is_multistep_goal("close discord, mute the volume, then open my email")


class _FakeEndpoint:
    def __init__(self, level=0.5, muted=False):
        self.lvl, self.mute = level, muted

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def level(self):
        return self.lvl

    def set_level(self, frac):
        self.lvl = frac

    def muted(self):
        return self.mute

    def set_muted(self, on):
        self.mute = on


@pytest.fixture
def speakers(monkeypatch):
    ep = _FakeEndpoint()
    monkeypatch.setattr(volume, "_Endpoint", ep)
    return ep


def test_set_up_down_and_get_move_the_master_volume(speakers):
    assert volume.control("set", 40) == "Volume set to 40%, sir."
    assert volume.control("up") == "Volume up to 50%, sir."
    assert volume.control("down", 30) == "Volume down to 20%, sir."
    assert volume.control("down", 90) == "Volume down to 0%, sir."          # clamped
    assert volume.control("set", "100%") == "Volume set to 100%, sir."
    assert volume.control("up", 20) == "Volume up to 100%, sir."
    assert volume.control("get") == "The volume is at 100%, sir."


def test_mute_unmute_and_a_louder_request_unmutes(speakers):
    assert volume.control("mute") == "Muted, sir." and speakers.mute
    assert volume.control("get") == "The volume is at 50%, muted, sir."
    assert volume.control("unmute") == "Unmuted, sir. The volume is at 50%." and not speakers.mute
    speakers.mute = True
    volume.control("up")
    assert not speakers.mute                     # asking for sound means hearing it
    speakers.mute = True
    volume.control("down")
    assert speakers.mute                         # quieter never unmutes


@pytest.mark.parametrize("action, percent", [("set", None), ("set", 150), ("louder", 10), ("up", "lots")])
def test_a_bad_request_says_what_is_wrong(speakers, action, percent):
    assert volume.control(action, percent).startswith("[Error]")
    assert speakers.lvl == 0.5


def test_without_core_audio_the_volume_keys_are_pressed(monkeypatch):
    def broken():
        raise OSError("no audio endpoint")
    presses = []
    monkeypatch.setattr(volume, "_Endpoint", broken)
    monkeypatch.setattr(volume, "_press", lambda key, times=1: presses.append((key, times)))
    assert volume.control("set", 30) == "Volume set to 30%, sir."
    assert presses == [("down", volume.KEY_PRESSES_FULL), ("up", 15)]
    presses.clear()
    assert volume.control("up", 20) == "Volume up, sir." and presses == [("up", 10)]
    presses.clear()
    assert volume.control("mute") == "Toggled mute, sir." and presses == [("mute", 1)]
    assert volume.control("get").startswith("[Error]")


def test_set_volume_is_a_tool_the_models_see_and_the_gate_lets_through(speakers):
    from cygnus import safety, tools
    from cygnus.brain.tool_schema import TOOL_DECLARATIONS
    assert "set_volume" in {t.name for t in TOOL_DECLARATIONS}
    assert safety.assess("set_volume", {"action": "up"}).kind == "act"
    assert tools.execute_tool("set_volume", {"action": "set", "percent": 70}) == "Volume set to 70%, sir."


def test_the_turn_changes_the_volume_at_once(speakers, monkeypatch):
    from cygnus.brain import agent_loop
    monkeypatch.setattr(agent_loop, "MODE", "off")
    brain = b.JarvisBrain()
    assert brain.think("Turn the volume up up to 100%.") == "Volume set to 100%, sir."
    assert brain.last_stats == {"backend": "instant", "intent": "volume"}
    assert speakers.lvl == 1.0
