"""wake_engine: the server-side wake fallback decodes at most once per step,
catches up on a backlog in one pass, stays quiet on silence, and holds off
while the browser records a command. No microphone: _tick() is fed directly."""

import time

import numpy as np
import pytest

from cygnus.voice.wake import WakeEngine, wake_targets, wake_word_in


@pytest.fixture(autouse=True)
def _no_debug_file(monkeypatch):
    """Tests never write to the real logs/wake_debug.log."""
    monkeypatch.setattr(WakeEngine, "_write_debug", lambda self, msg: None)


# --- who a server wake is delivered to -------------------------------------

def test_remote_clients_never_get_a_server_wake():
    assert set(wake_targets({"tab", "phone"}, remote={"phone"}, desktop=set())) == {"tab"}


def test_the_desktop_window_wins_over_a_local_tab():
    assert wake_targets({"tab", "desk"}, remote=set(), desktop={"desk"}) == ["desk"]


def test_without_the_desktop_app_every_local_tab_gets_it():
    assert set(wake_targets({"tab1", "tab2"}, remote=set(), desktop=set())) == {"tab1", "tab2"}


def test_a_remote_client_claiming_desktop_is_ignored():
    assert wake_targets({"tab", "phone"}, remote={"phone"}, desktop={"phone"}) == ["tab"]


# --- a microphone stream that went silent is reopened ----------------------

class _FakeStream:
    opened = 0

    def __init__(self, **kwargs):
        type(self).opened += 1

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def _silent_engine(monkeypatch, open_stream):
    from cygnus.voice import wake as wake_engine
    _FakeStream.opened = 0
    monkeypatch.setattr(wake_engine.sd, "InputStream", open_stream)
    engine = WakeEngine(voice_engine=None)
    engine._last_audio_ts = 1000.0          # the last audio block arrived at t=1000
    return engine


def test_a_silent_stream_is_reopened_at_most_every_ten_seconds(monkeypatch):
    engine = _silent_engine(monkeypatch, _FakeStream)
    engine._maybe_reopen(1003.0)            # 3 s of nothing: still just a pause
    assert _FakeStream.opened == 0
    engine._maybe_reopen(1006.0)            # past 5 s: the stream died, reopen it
    assert _FakeStream.opened == 1
    assert engine.last_err == "no microphone audio received"
    engine._maybe_reopen(1010.0)            # 4 s after that try: wait
    assert _FakeStream.opened == 1
    engine._maybe_reopen(1016.5)            # 10.5 s after it: try again
    assert _FakeStream.opened == 2


def test_a_device_that_will_not_open_is_reported_not_raised(monkeypatch):
    def refuse(**kwargs):
        raise OSError("device gone")
    engine = _silent_engine(monkeypatch, refuse)
    engine._maybe_reopen(1006.0)
    assert engine.healthy is False
    assert "device gone" in engine.last_err


BLOCK = 1600          # 0.1 s at 16 kHz, what the audio callback delivers


class _Voice:
    def __init__(self, text=""):
        self.calls, self.text = 0, text

    def transcribe_wake(self, window):
        self.calls += 1
        return self.text


def _loud(value=0.05):
    return np.full(BLOCK, value, dtype=np.float32)


class _Spotter:
    name = "sherpa"

    def __init__(self, hit=True):
        self.calls, self.hit = 0, hit

    def hears(self, window, sample_rate=16000):
        self.calls += 1
        return "hey cygnus" if self.hit else ""


def test_a_keyword_spotter_hears_the_window_instead_of_whisper():
    voice, spotter = _Voice("not used"), _Spotter()
    engine = WakeEngine(voice, spotter=spotter)
    woke = []
    engine.set_callback(lambda: woke.append(1))
    for i in range(30):
        engine._tick([_loud()], 1000.0 + i / 10)
    assert spotter.calls == 1 and voice.calls == 0 and woke == [1]


def test_the_sherpa_spotter_hears_the_recorded_wake_clips_and_not_the_talk():
    """The real model on the owner's clips, when both are present."""
    import os
    import wave
    pytest.importorskip("sherpa_onnx")
    from cygnus.voice import wake_kws
    data = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval", "voice_testset")
    if not os.path.isdir(wake_kws.DEFAULT_DIR) or not os.path.exists(os.path.join(data, "01_wake.wav")):
        pytest.skip("no keyword model or recorded clips here")
    spotter = wake_kws.SherpaSpotter()

    def load(name):
        with wave.open(os.path.join(data, name)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    heard = sum(1 for i in range(1, 21) if spotter.hears(load(f"{i:02d}_wake.wav")))
    assert heard >= 15
    assert spotter.hears(load("42_negative.wav")) == ""          # sinus, sign us up, signal


def test_music_decodes_once_per_step_not_once_per_block():
    voice = _Voice()
    engine = WakeEngine(voice)
    start = 1000.0                       # far past the initial cooldown
    for i in range(50):                  # 5 s of steady music, block by block
        engine._tick([_loud()], start + i / 10)
    # The 2.5 s window is full at block 25; then one decode per second.
    assert voice.calls == 3


def test_a_backlog_is_folded_in_at_once_and_keeps_the_newest_audio():
    voice = _Voice()
    engine = WakeEngine(voice)
    backlog = [_loud(0.01)] * 39 + [_loud(0.09)]              # 4 s piled up
    assert engine._tick(backlog, 1000.0) is True
    assert voice.calls == 1                                   # one decode, not 40
    assert engine._buf.shape[0] == int(engine.sr * (engine.window_s + engine.step_s))
    assert engine._buf[-1] == np.float32(0.09)


def test_silence_never_reaches_whisper():
    voice = _Voice()
    engine = WakeEngine(voice)
    for i in range(50):
        engine._tick([np.zeros(BLOCK, dtype=np.float32)], 1000.0 + i / 10)
    assert voice.calls == 0


def test_hold_while_the_browser_records_then_release():
    voice = _Voice()
    engine = WakeEngine(voice)
    engine._tick([_loud()] * 25, 1000.0)
    engine.hold(30000)
    now = time.time()
    assert engine._tick([_loud()], now) is False and voice.calls == 1
    engine.release()
    assert engine._tick([_loud()], now + 0.1) is True and voice.calls == 2


def test_release_does_not_cut_the_post_speech_suspend_short():
    engine = WakeEngine(_Voice())
    engine.suspend(5000)
    engine.release()
    assert engine._tick([_loud()] * 25, time.time()) is False


def test_wake_word_fires_the_callback_once_per_cooldown():
    voice = _Voice("cygnus")
    engine = WakeEngine(voice)
    fired = []
    engine.set_callback(lambda: fired.append(1))
    for i in range(40):
        engine._tick([_loud()], 1000.0 + i / 10)
    assert fired == [1]                   # 4 s cooldown after the first wake


# --- the debug log: what was heard, and why nothing was decoded ------------

def _logged(engine):
    lines = []
    engine._log = lines.append
    return lines


def test_a_miss_is_logged_with_what_whisper_heard():
    engine = WakeEngine(_Voice("more color signals"))
    lines = _logged(engine)
    engine._tick([_loud()] * 25, 1000.0)
    assert any("heard 'more color signals'" in line and "WAKE" not in line for line in lines)


def test_a_hit_is_logged_with_whether_a_hud_got_it():
    engine = WakeEngine(_Voice("Hey Cygnus."))
    lines = _logged(engine)
    engine.set_callback(lambda: False)    # no HUD connected
    engine._tick([_loud()] * 25, 1000.0)
    assert any("heard 'Hey Cygnus.'" in line and "-> WAKE" in line for line in lines)
    assert "wake NOT delivered: no HUD connected" in lines


def test_why_it_is_not_decoding_is_logged_once_per_change():
    engine = WakeEngine(_Voice())
    lines = _logged(engine)
    engine.pause()
    for i in range(30):
        engine._tick([_loud()], 1000.0 + i / 10)
    engine.resume()
    engine._tick([_loud()], 1003.1)
    assert [line for line in lines if line.startswith("listener:")] == [
        "listener: paused: a command turn is running", "listener: decoding"]


# --- what counts as the wake word, and whose voice said it ------------------

@pytest.mark.parametrize("text, wake", [
    ("signals.", True), ("Sign us.", True), ("Hey, Seagmus.", True), ("Hey Cygnus.", True),
    ("more color signals.", False), ("the input of the signal.", False), ("Sign this.", False)])
def test_whispers_other_spellings_count_only_on_their_own(text, wake):
    assert wake_word_in(text) is wake


def test_its_own_reply_saying_the_wake_word_does_not_wake_it():
    engine = WakeEngine(_Voice("Cygnus."))
    lines = _logged(engine)
    fired = []
    engine.set_callback(lambda: fired.append(1))
    engine.note_spoken("I'm Cygnus, sir.")
    engine.speaking(True)
    engine._tick([_loud()] * 25, time.time())
    assert fired == []
    assert any("its own reply says the wake word" in line for line in lines)


def test_the_owner_cutting_in_while_it_talks_still_wakes_it():
    engine = WakeEngine(_Voice("Hey Cygnus."))
    fired = []
    engine.set_callback(lambda: fired.append(1))
    engine.note_spoken("The weather is sunny today, sir.")
    engine.speaking(True)
    engine._tick([_loud()] * 25, time.time())
    assert fired == [1]
