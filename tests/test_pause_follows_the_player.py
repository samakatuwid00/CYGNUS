""""pause the music" pauses whatever Cygnus last started: after
play_spotify it said "Nothing is playing - the music window is not open"
and Spotify kept playing (live smoke, 2026-09-22)."""

from cygnus.agents import music as music_agent
from cygnus import tools


def test_pause_after_spotify_pauses_spotify(monkeypatch):
    monkeypatch.setattr(tools, "play_spotify", lambda q: f"Playing {q} on Spotify, sir.")
    monkeypatch.setattr(tools, "stop_spotify", lambda: "Paused Spotify, sir.")
    monkeypatch.setattr(music_agent, "stop_music", lambda: "Nothing is playing - the music window is not open.")
    tools.TOOL_MAP["play_spotify"](query="lo-fi")
    assert tools.TOOL_MAP["stop_music"]() == "Paused Spotify, sir."
    # once: a second pause must not press play/pause again and restart it
    assert tools.TOOL_MAP["stop_music"]() == "Nothing is playing - the music window is not open."


def test_pause_after_the_browser_player_stops_the_browser(monkeypatch):
    monkeypatch.setattr(music_agent, "play_music", lambda q: f"Playing {q}.")
    monkeypatch.setattr(music_agent, "stop_music", lambda: "Music stopped.")
    monkeypatch.setattr(tools, "stop_spotify", lambda: (_ for _ in ()).throw(AssertionError("not spotify")))
    tools.TOOL_MAP["play_music"](query="lo-fi")
    assert tools.TOOL_MAP["stop_music"]() == "Music stopped."


def test_a_failed_spotify_play_is_not_remembered(monkeypatch):
    monkeypatch.setattr(tools, "play_spotify", lambda q: "[Error] Spotify is not installed.")
    monkeypatch.setattr(tools, "stop_spotify", lambda: (_ for _ in ()).throw(AssertionError("not spotify")))
    monkeypatch.setattr(music_agent, "stop_music", lambda: "Nothing is playing - the music window is not open.")
    tools._LAST_PLAYER["name"] = None
    tools.TOOL_MAP["play_spotify"](query="lo-fi")
    assert tools.TOOL_MAP["stop_music"]().startswith("Nothing is playing")
