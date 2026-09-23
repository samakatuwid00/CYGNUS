"""JARVIS_LOCAL_ONLY has to mean it.

docs/WHAT_IT_CAN_DO.md said "with Ollama and JARVIS_PREFER_LOCAL=true, nothing
leaves at all". That was not true: the default voice is edge-tts, which opens a
socket to Microsoft's speech service and uploads the full text of every reply -
including whatever Cygnus reads back from files, the vault or memory. A user
who asked for local only got a local brain and a cloud voice (found 2026-09-23
while writing docs/PRIVACY.md).
"""

import asyncio

import pytest

from cygnus import config
from cygnus.server import speech


@pytest.fixture(autouse=True)
def no_local_voice(monkeypatch):
    """No piper, no kokoro, and a voice-enabled server: the path that used to
    fall through to edge-tts."""
    monkeypatch.setattr(speech, "_get_piper", lambda: None)
    monkeypatch.setattr(speech, "_get_kokoro", lambda: None)
    monkeypatch.setattr(speech, "_TTS_ENGINE", "edge")
    from cygnus.server import app as server_app
    monkeypatch.setattr(server_app, "NO_VOICE", False)


def test_local_only_refuses_the_cloud_voice(monkeypatch):
    monkeypatch.setattr(config, "JARVIS_LOCAL_ONLY", True)

    def explode(*a, **k):                      # edge_tts must not be reached
        raise AssertionError("edge-tts was called under JARVIS_LOCAL_ONLY")
    monkeypatch.setattr("edge_tts.Communicate", explode, raising=False)

    assert asyncio.run(speech.tts_to_b64("the vault says your salary is X")) is None


def test_without_local_only_the_cloud_voice_is_used(monkeypatch):
    """The default is unchanged: edge-tts still speaks when it was not refused."""
    monkeypatch.setattr(config, "JARVIS_LOCAL_ONLY", False)
    called = {}

    class FakeCommunicate:
        def __init__(self, text, voice, **kw):
            called["text"] = text

        async def save(self, path):
            with open(path, "wb") as f:
                f.write(b"fake-mp3")

    monkeypatch.setattr("edge_tts.Communicate", FakeCommunicate, raising=False)
    out = asyncio.run(speech.tts_to_b64("hello"))
    assert out and called["text"] == "hello"
