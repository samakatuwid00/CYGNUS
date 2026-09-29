"""Only the owner's voice wakes Cygnus (voiceprint.py), with a fake extractor."""

import json
import types

import numpy as np
import pytest

from cygnus.voice import voiceprint, wake


@pytest.fixture
def vp(tmp_path, monkeypatch):
    """Embeddings from the signal's frequency: 220 Hz is 'the owner'."""
    def embed(audio, sr=16000):
        spec = np.abs(np.fft.rfft(np.asarray(audio, dtype=np.float64)[:sr]))
        v = np.array([spec[200:240].sum(), spec[430:470].sum(), 1e-3])
        return v / np.linalg.norm(v)
    monkeypatch.setattr(voiceprint, "embed", embed)
    monkeypatch.setattr(voiceprint, "VOICEPRINT", tmp_path / "voiceprint.json")
    monkeypatch.setattr(voiceprint, "MODEL", str(tmp_path / "model.onnx"))
    (tmp_path / "model.onnx").write_bytes(b"x")
    monkeypatch.setattr(voiceprint, "ENABLED", True)
    monkeypatch.setattr(voiceprint, "_enrolled", None)
    return voiceprint


def tone(hz, amp=0.2, s=2.0):
    t = np.arange(int(16000 * s)) / 16000
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_no_voiceprint_lets_every_voice_through(vp):
    assert vp.check(tone(450)) == (True, None)
    assert not vp.active()


def test_enrolled_owner_passes_and_a_stranger_does_not(vp):
    rec = vp.enroll([tone(220), tone(220, 0.1)], source="test")
    assert vp.VOICEPRINT.exists() and rec["clips"] == 2
    assert "vector" in json.loads(vp.VOICEPRINT.read_text()) and vp.active()
    ok, score = vp.check(tone(220, 0.05))
    assert ok and score > 0.9
    ok, score = vp.check(tone(450))
    assert not ok and score < vp.THRESHOLD


def test_switched_off_lets_every_voice_through(vp, monkeypatch):
    vp.enroll([tone(220)])
    monkeypatch.setattr(vp, "ENABLED", False)
    assert vp.check(tone(450)) == (True, None)


def test_trim_keeps_the_speech_and_drops_the_silence():
    a = np.concatenate([np.zeros(16000, np.float32), tone(220, s=0.5), np.zeros(16000, np.float32)])
    t = voiceprint.trim(a)
    assert 0.5 * 16000 <= len(t) <= 0.9 * 16000


def _engine(said):
    class Spotter:
        name = "fake"

        def hears(self, window, sr):
            return said
    e = wake.WakeEngine(voice_engine=None, spotter=Spotter())
    fired, logged = [], []
    e.set_callback(lambda: fired.append(1))
    e._log = logged.append
    return e, fired, logged


def test_wake_engine_ignores_a_stranger_saying_cygnus(vp):
    vp.enroll([tone(220)])
    e, fired, logged = _engine("hey cygnus")
    for i in range(30):
        e._tick([tone(450, s=0.1)], 1000.0 + i / 10)
    assert fired == [] and any("not the owner's voice" in m for m in logged)


def test_wake_engine_wakes_for_the_owner(vp):
    vp.enroll([tone(220)])
    e, fired, logged = _engine("hey cygnus")
    for i in range(30):
        e._tick([tone(220, s=0.1)], 1000.0 + i / 10)
    assert fired and any("-> WAKE" in m and "voice" in m for m in logged)


def test_a_broken_voice_check_never_blocks_the_wake(vp, monkeypatch):
    vp.enroll([tone(220)])
    monkeypatch.setattr(vp, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model gone")))
    e, fired, logged = _engine("hey cygnus")
    for i in range(30):
        e._tick([tone(450, s=0.1)], 1000.0 + i / 10)
    assert fired and any("waking anyway" in m for m in logged)
