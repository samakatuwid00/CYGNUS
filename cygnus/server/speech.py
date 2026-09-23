"""cygnus.server.speech - turning a reply into speech: the last cleanup of the
text, the TTS engines (edge, Piper, Kokoro) to base64 audio, and the
failsafe that restores music a lost speech-end left ducked.

Moved out of server/app.py (restructure phase 5)."""

import asyncio
import base64
import os
import tempfile
import threading
import time

from cygnus import paths
from cygnus.voice import ducking as voice_ducking
from cygnus.voice import feedback as _vf
from cygnus.server.app import wake_engine  # created before this module is imported


# The anti-AI-ism filter (machine tics, em dashes) lives in plain_reply.tidy
# with the rest of the outgoing-text cleanup, where it is tested.


def strip_ai_artifacts(text: str) -> str:
    """Strip the worst P0 AI-isms from a reply before it is spoken.

    Removes chatbot artifacts and filler openers entirely, and converts em dashes /
    double-hyphens to a comma so speech does not stumble on a dash. Leaves already-clean,
    brisk replies untouched. Does not alter meaning."""
    # Internal markers, job ids, Markdown and machine tics out (plain_reply.py).
    # Only here, where replies leave the server: brain.think's own return value
    # keeps its markers for the command chain.
    from cygnus.brain import plain_reply
    return plain_reply.tidy(text)


# ---------------------------------------------------------------------------
# Phase 16.6 REV A (2026-08-25): edge-tts RESTORED as PRIMARY — the user wants
# the original en-GB-Ryan voice back. Piper stays available but OPT-IN only:
# set JARVIS_TTS_ENGINE=piper to use it (real-time offline, RTF 0.12 measured).
_TTS_ENGINE = (os.environ.get("JARVIS_TTS_ENGINE") or "edge").strip().lower()
_PIPER_MODEL = os.path.join(os.path.expanduser("~"), ".jarvis-tts",
                            "en_GB-nem-medium.onnx")
_piper_voice = None
_KOKORO_DIR = str(paths.MODELS_DIR / "kokoro")
_KOKORO_TTS = None


def _get_piper():
    """Load the piper voice once; return None on any failure (edge fallback)."""
    global _piper_voice
    if _piper_voice is not None:
        return _piper_voice
    try:
        from piper import PiperVoice
        if os.path.exists(_PIPER_MODEL):
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                _piper_voice = ex.submit(PiperVoice.load, _PIPER_MODEL) \
                    .result(timeout=60)
            return _piper_voice
    except Exception as e:
        print(f"[TTS] piper unavailable ({e}); using edge-tts fallback.")
        _piper_voice = None
    return None


def _get_kokoro():
    """Load the Kokoro ONNX TTS once; return None on any failure (edge fallback)."""
    global _KOKORO_TTS
    if _KOKORO_TTS is not None:
        return _KOKORO_TTS
    try:
        from kokoro_onnx import Kokoro
        model = os.path.join(_KOKORO_DIR, "kokoro-v1.0.onnx")
        voices = os.path.join(_KOKORO_DIR, "voices-v1.0.bin")
        if os.path.exists(model) and os.path.exists(voices):
            import numpy as _np
            _np_load = _np.load
            _np.load = lambda *a, **k: _np_load(*a, allow_pickle=True, **k)
            _KOKORO_TTS = Kokoro(model, voices)
            _np.load = _np_load

            # kokoro-onnx 0.4.7 (latest) feeds `speed` as int32 while
            # kokoro-v1.0.onnx declares it float, so every synth died with
            #   INVALID_ARGUMENT ... Actual: (tensor(int32)), expected: (tensor(float))
            # and JARVIS silently fell back to edge-tts — which is why
            # JARVIS_TTS_ENGINE=kokoro never actually took effect. Coerce each
            # feed to the dtype the graph declares. This is a no-op once the
            # library is fixed upstream, and adapts to either export variant.
            _sess = _KOKORO_TTS.sess
            _raw_run = _sess.run
            _want = {i.name: i.type for i in _sess.get_inputs()}

            def _run_coerced(output_names, input_feed, *a, **kw):
                fixed = {}
                for _name, _val in input_feed.items():
                    _t = _want.get(_name)
                    if _t == "tensor(float)":
                        fixed[_name] = _np.asarray(_val, dtype=_np.float32)
                    elif _t == "tensor(int64)":
                        fixed[_name] = _np.asarray(_val, dtype=_np.int64)
                    else:
                        fixed[_name] = _val
                return _raw_run(output_names, fixed, *a, **kw)

            _sess.run = _run_coerced
            print("[TTS] Kokoro (local, offline) ready — JARVIS_TTS_ENGINE=kokoro")
            return _KOKORO_TTS
        print(f"[TTS] kokoro model missing in {_KOKORO_DIR}; using edge-tts fallback.")
    except Exception as e:
        print(f"[TTS] kokoro unavailable ({e}); using edge-tts fallback.")
        _KOKORO_TTS = None
    return None


async def tts_to_b64(text: str) -> str:
    """Synthesize `text` and return base64 audio.

    DEFAULT: edge-tts MP3 (the original JARVIS voice). Opt-in piper WAV via
    JARVIS_TTS_ENGINE=piper; falls back to edge automatically on any failure.
    """
    from cygnus.server.app import NO_VOICE
    if NO_VOICE:
        return ""                      # typed turns only: nothing is spoken
    text = strip_ai_artifacts(text)
    try:
        wake_engine.note_spoken(text)   # a wake in its own voice is not the owner's
    except Exception:
        pass
    if _TTS_ENGINE == "kokoro":
        kokoro = _get_kokoro()
        if kokoro is not None:
            try:
                def _synth():
                    import re as _re
                    import numpy as _np2
                    samples, sr = kokoro.create(
                        text, voice="am_michael", speed=1.1, lang="en-us")
                    import io as _io
                    import soundfile as _sf
                    buf = _io.BytesIO()
                    _sf.write(buf, _np2.asarray(samples), sr, format="WAV")
                    return buf.getvalue()
                wav_bytes = await asyncio.to_thread(_synth)
                return base64.b64encode(wav_bytes).decode()
            except Exception as e:
                print(f"[TTS] kokoro synth failed ({e}); falling back to edge-tts.")
    if _TTS_ENGINE == "piper":
        voice = _get_piper()
        if voice is not None:
            try:
                import wave
                wav_path = tempfile.NamedTemporaryFile(suffix=".wav",
                                                       delete=False).name
                # CPU synth blocks ~0.1-0.5s: keep the event loop responsive.
                def _synth():
                    with wave.open(wav_path, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(22050)
                        voice.synthesize_wav(text, wf)
                await asyncio.to_thread(_synth)
                with open(wav_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                os.unlink(wav_path)
                return b64
            except Exception as e:
                print(f"[TTS] piper synth failed ({e}); falling back to edge-tts.")
    import edge_tts
    from cygnus.config import TTS_VOICE, TTS_RATE, TTS_VOLUME

    # rate/volume were previously dropped here, so the web UI always spoke at
    # +0% no matter what config said — only the CLI path honoured TTS_RATE.
    communicate = edge_tts.Communicate(text, TTS_VOICE, rate=TTS_RATE, volume=TTS_VOLUME)
    mp3_path = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    await communicate.save(mp3_path)
    try:
        with open(mp3_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    finally:
        os.unlink(mp3_path)


_vf.set_synthesizer(tts_to_b64)
if _TTS_ENGINE == "kokoro" and _get_kokoro() is not None:
    _vf.warm_cache()


def _duck_failsafe_loop():
    """Periodically restore ducked music if a speech-end message was lost."""
    while True:
        time.sleep(30)
        try:
            voice_ducking.check_failsafe()
        except Exception:
            pass


threading.Thread(target=_duck_failsafe_loop, daemon=True, name="duck-failsafe").start()
