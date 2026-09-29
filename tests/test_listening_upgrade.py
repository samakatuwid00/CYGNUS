"""The listening fixes of 2026-09-26: the pre-rolled clip's command, the
intake corpus learning the user's app names, and ducking music while
Cygnus listens."""

import sys
import types

import pytest

from cygnus.brain import intake
from cygnus.voice import ducking
from cygnus.voice.wake import after_wake


# ------------------------------------------------------------ after_wake --

@pytest.mark.parametrize("heard, command", [
    ("Cygnus, open Spotify.", "open Spotify."),
    ("Hey Cygnus, open Spotify", "open Spotify"),
    ("Cygnus, Cygnus, Cygnus, what is the percentage of my battery?",
     "what is the percentage of my battery?"),
    # the pre-roll holds what was said before the wake: not the command
    ("Pause the music. Cygnus, next song.", "next song."),
    ("Signus play some music", "play some music"),
    ("Cygnus tell me about the Cygnus constellation", "tell me about the Cygnus constellation"),
    ("Cygnus.", ""),
])
def test_after_wake_keeps_what_follows_the_wake_word(heard, command):
    assert after_wake(heard) == command


@pytest.mark.parametrize("heard", ["Open Spotify.", "the design signals are good", "",
                                   "Sign up for the course", "Science class is over",
                                   "Close the signals app", "Cygnets are baby swans",
                                   "Open the census report", "My sinuses hurt"])
def test_after_wake_leaves_text_without_a_wake_word(heard):
    assert after_wake(heard) == heard


@pytest.mark.parametrize("heard, command", [
    ("Sigeonos, open Spotify.", "open Spotify."),     # heard end to end, 2026-09-26
    ("Sigeoness, open Spotify.", "open Spotify."),
    ("The signals are fine. Seagnus, next song", "next song"),
    # the owner's "Hey Cygnus", as Whisper wrote it from the voice test set
    ("Pacing us. Pause the music on Spotify.", "Pause the music on Spotify."),
    ("Pacingness. Pause the music on Spotify.", "Pause the music on Spotify."),
    ("Pay signals, open Spotify.", "open Spotify."),
    ("Asygnus, up. Open Docker Desktop.", "up. Open Docker Desktop."),
])
def test_after_wake_catches_rarer_spellings(heard, command):
    assert after_wake(heard) == command


# ------------------------------------------------------------ with_names --

def test_with_names_adds_registered_names_to_apps():
    corpus = {"apps": ["notepad", "chrome"], "commands": ["open"]}
    merged = intake.with_names(corpus, {"claude", "Chrome", "docker desktop"})
    assert "claude" in merged["apps"] and "docker" in merged["apps"]
    assert merged["apps"].count("chrome") == 1
    assert corpus["apps"] == ["notepad", "chrome"]          # the loaded corpus is not changed


def test_with_names_drops_generic_words():
    merged = intake.with_names({"apps": []}, {"youtube music", "xampp control panel", "music"})
    assert "youtube" in merged["apps"] and "xampp" in merged["apps"]
    assert not any(w in a.split() for a in merged["apps"] for w in ("music", "panel", "control"))


def test_with_names_resolves_a_registered_app():
    corpus = intake.with_names(intake.load_corpus(), {"claude", "docker desktop"})
    assert intake.resolve_intent("can you open claude desktop", corpus).entity == "claude"
    assert intake.resolve_intent("Open Docker Desktop.", corpus).entity == "docker"
    assert intake.resolve_intent("stop the music", corpus).entity is None


# --------------------------------------------------------------- ducking --

class _Vol:
    def __init__(self, level):
        self.level = level

    def GetMasterVolume(self):
        return self.level

    def SetMasterVolume(self, level, _ctx):
        self.level = level


class _Session:
    def __init__(self, name, pid, level=1.0):
        self.Process = types.SimpleNamespace(name=lambda: name) if name else None
        self.ProcessId = pid
        self.SimpleAudioVolume = _Vol(level)


@pytest.fixture
def sessions(monkeypatch):
    live = [_Session("Spotify.exe", 1, 0.8), _Session("brave.exe", 2), _Session("electron.exe", 3),
            _Session(None, 0)]
    pycaw = types.ModuleType("pycaw")
    pycaw.pycaw = types.SimpleNamespace(AudioUtilities=types.SimpleNamespace(GetAllSessions=lambda: live))
    monkeypatch.setitem(sys.modules, "pycaw", pycaw)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", pycaw.pycaw)
    ducking._reset_for_tests()
    yield {s.ProcessId: s.SimpleAudioVolume for s in live}
    ducking._reset_for_tests()


def test_speech_ducks_spotify_not_browsers(sessions):
    assert ducking.duck() is True
    assert sessions[1].level == ducking.DUCK_VOLUME
    assert sessions[2].level == 1.0 and sessions[3].level == 1.0


def test_listening_ducks_browsers_too_but_never_the_hud(sessions):
    assert ducking.duck(browsers=True) is True
    assert sessions[1].level == ducking.DUCK_VOLUME and sessions[2].level == ducking.DUCK_VOLUME
    assert sessions[3].level == 1.0             # electron: the desktop HUD's own voice


def test_second_duck_adds_browsers_and_unduck_restores_all(sessions):
    ducking.duck()
    ducking.duck(browsers=True)
    assert sessions[2].level == ducking.DUCK_VOLUME
    ducking.unduck()
    assert sessions[1].level == 0.8 and sessions[2].level == 1.0


def test_duck_without_pycaw_does_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "pycaw", None)
    ducking._reset_for_tests()
    assert ducking.duck(browsers=True) is False
    ducking.unduck()


def test_unduck_restores_a_session_the_browser_replaced(monkeypatch):
    """Chrome replaced its audio session while ducked; Windows gave the new one
    the ducked level. Restoring through the old handle left it at 15%."""
    live = [_Session("chrome.exe", 7, 1.0)]
    pycaw = types.ModuleType("pycaw")
    pycaw.pycaw = types.SimpleNamespace(AudioUtilities=types.SimpleNamespace(GetAllSessions=lambda: live))
    monkeypatch.setitem(sys.modules, "pycaw", pycaw)
    monkeypatch.setitem(sys.modules, "pycaw.pycaw", pycaw.pycaw)
    ducking._reset_for_tests()
    ducking.duck(browsers=True)
    live[0] = _Session("chrome.exe", 7, ducking.DUCK_VOLUME)     # the replacement
    ducking.unduck()
    assert live[0].SimpleAudioVolume.level == 1.0
    ducking._reset_for_tests()


# ------------------------------------------------------------- wake gain --

def test_wake_gain_lifts_a_quiet_voice_but_never_lowers_or_over_boosts():
    import numpy as np
    from cygnus.voice import wake
    sr = 16000
    t = np.arange(sr * 2) / sr
    quiet = (0.004 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)        # rms ~0.0028
    out = wake.wake_gain(quiet, sr)
    assert np.abs(out).max() == pytest.approx(np.abs(quiet).max() * wake.WAKE_GAIN_MAX, rel=1e-3)
    loud = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    assert wake.wake_gain(loud, sr) is loud                                  # never quieter
    silent = np.zeros(sr * 2, np.float32)
    assert wake.wake_gain(silent, sr) is silent


def test_spotter_hears_a_window_the_old_gate_dropped(monkeypatch):
    """A far-away "Hey Cygnus" (loudest slice ~0.001) reaches the spotter, gained."""
    import numpy as np
    from cygnus.voice import wake
    heard = []

    class Spotter:
        name = "fake"

        def hears(self, window, sr):
            heard.append(float(np.abs(window).max()))
            return ""
    engine = wake.WakeEngine(voice_engine=None, spotter=Spotter())
    sr, t = 16000, np.arange(1600) / 16000
    block = (0.0016 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)       # mean |x| ~0.001
    for i in range(30):
        engine._tick([block], 1000.0 + i / 10)
    assert heard and heard[0] > 0.0016 * 4                                   # decoded, and louder
    engine_w = wake.WakeEngine(voice_engine=None)                            # Whisper keeps the old gate
    calls = []
    engine_w.ve = type("V", (), {"transcribe_wake": lambda self, w: calls.append(1) or ""})()
    for i in range(30):
        engine_w._tick([block], 1000.0 + i / 10)
    assert calls == []
