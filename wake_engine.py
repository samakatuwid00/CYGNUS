"""Server-side wake-word listener for JARVIS.

Why this exists
---------------
The browser's Web Speech API (WakeListener in jarvis_visual.html) is the PRIMARY
wake detector, but it is fragile:
  * Chrome/Edge-only (no Firefox).
  * It is a cloud/on-device speech engine that degrades badly when music/ambient
    noise bleeds into the mic — exactly the "JARVIS won't hear me over music"
    symptom. The browser just never fires the wake, and there is no fallback.

This engine provides that fallback. It runs a continuous microphone capture in
the *server* process (which already has Whisper loaded) and transcribes short
rolling windows with Whisper, which we empirically verified hears "jarvis" cleanly
even over a 2x music bed. When it detects the wake word it calls a callback so
the server can tell the browser to open its command window.

Design notes / pitfalls
-----------------------
* It must NOT react to JARVIS's own TTS. We pass a `suspend()` window after each
  speak so the engine goes silent while JARVIS is talking. Music is NOT TTS, so
  music does not falsely suspend the listener (that would defeat the whole point).
* Debounced: once it fires it won't fire again for WAKE_COOLDOWN_MS, so one
  "jarvis" doesn't open twenty captures.
* The browser already does its own wake detection; to avoid double capture the
  server only acts when no browser-side wake has recently happened. The server
  tracks the last browser-wake time via `note_browser_wake()` (called from the WS
  handler when the browser's own listener fires).
* Whisper is CPU-heavy. This loop decodes at most once per step_s for as
  long as the room is above the silence gate. Before 2026-09-11 nothing
  enforced the step: it decoded after every 0.1 s block, so music kept two
  cores busy nonstop and starved the command transcription. Audio that piled
  up during a decode is folded in at once (the buffer keeps the newest
  window), so the listener never falls behind. It runs its own tiny model on
  few threads (WAKE_WHISPER_MODEL), pauses while a command turn is in flight,
  and holds off while the browser is recording a command (hold()/release()).
"""

import os
import time
import queue
import threading
from collections import deque

import logging
from logging.handlers import RotatingFileHandler

import numpy as np
import sounddevice as sd

import re
import paths

# The wake word is "Cygnus", matched the way Whisper actually writes it:
# Cygnus, Signus, Signis, Signess, Signes, Signez(z), Signeze, and "Heysignas"
# run together (measured on five edge-tts voices, UK/US/PH, 2026-09-12). The
# g and the vowel are required, so "signs", "signal", "sinus" and "sickness"
# never count. The HUD's WAKE_RE is the same pattern.
WAKE_RE = re.compile(r"(?:^|[\s,])(?:hey)?[cs][iy]gn[aeiu][sz]{1,2}e?(?=$|[\s.,!?;:])", re.I)
# Whisper's other spellings of it, heard from the owner in logs/wake_debug.log
# (2026-09-13): "signals", "Sign us", "Seagmus". "signals" is also a real word
# ("more color signals"), so these count only as the whole utterance, with or
# without "hey". The HUD's WAKE_BARE_RE is the same pattern.
WAKE_BARE_RE = re.compile(r"^(?:hey[\s,]*)?(?:signals?|sign\s*us|seagmus|sickness)$", re.I)

# Silence gate: mean |amplitude| of the loudest BURST_S slice of the window must
# reach this floor or the window is skipped. Measured on the loudest slice, not
# the whole window: in a quiet room a spoken "jarvis" fills ~0.5 s of the 2.5 s
# window, so the whole-window mean diluted soft voices under the floor. Steady
# room tone has no loud slice, so it is still skipped.
SILENCE_FLOOR = float(os.environ.get("JARVIS_WAKE_SILENCE_FLOOR", "0.002"))
BURST_S = 0.5
# No audio for this long means the stream died (sleep, a device unplugged or
# taken), and a dead stream is reopened at most this often.
SILENT_STREAM_S = 5.0
REOPEN_EVERY_S = 10.0
# The listener hears Cygnus's own voice. While a reply plays, a wake counts
# unless something said in the last ECHO_WINDOW_S holds the wake word (then it
# is the echo, not the owner cutting in). A lost "stopped speaking" ends the
# speaking state after SPEAKING_CAP_S.
ECHO_WINDOW_S = 120.0
SPEAKING_CAP_S = 90.0

# Every decoded window, hit or miss, and every change in why the listener is
# not decoding go to logs/wake_debug.log, so a missed wake shows what Whisper
# heard ("signals"?) or that the listener was held off. The server adds what
# the HUD did with each wake. On unless JARVIS_WAKE_DEBUG=0; 1 MB, one old
# file kept.
WAKE_DEBUG = os.environ.get("JARVIS_WAKE_DEBUG", "1") != "0"
WAKE_DEBUG_LOG = paths.log("wake_debug.log")


def _debug_logger():
    log = logging.getLogger("wake_debug")
    if not log.handlers:
        os.makedirs(os.path.dirname(WAKE_DEBUG_LOG), exist_ok=True)
        handler = RotatingFileHandler(WAKE_DEBUG_LOG, maxBytes=1_000_000,
                                      backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def wake_word_in(text):
    """Mirror of the HUD's wakeWordIn() so server and browser agree on what the
    wake word is."""
    if not text:
        return False
    t = text.strip()
    return bool(WAKE_RE.search(t) or WAKE_BARE_RE.match(re.sub(r"[^\w\s]", "", t).strip()))


def wake_targets(clients, remote, desktop):
    """Which HUD clients a server-side wake goes to. Remote clients (the phone
    through tailscale serve) are in another room and never get one. While the
    desktop app's window is connected it alone does, so a Chrome tab on the
    same PC does not open a second capture for the same "jarvis"."""
    local = [c for c in clients if c not in remote]
    return [c for c in local if c in desktop] or local


class WakeEngine:
    def __init__(self, voice_engine, sample_rate=16000,
                 window_s=2.5, step_s=1.0, cooldown_ms=4000,
                 suspend_ms=1500, device=None, spotter=None):
        self.ve = voice_engine
        # A keyword spotter (wake_kws.SherpaSpotter) hears the window instead
        # of Whisper when given: 17/20 of the owner's clips against 15, in a
        # fifteenth of the time (2026-09-15). None keeps Whisper.
        self.spotter = spotter
        self.sr = sample_rate
        self.window_s = window_s
        self.step_s = step_s
        self.cooldown = cooldown_ms / 1000.0
        self.suspend_s = suspend_ms / 1000.0
        self.device = device

        self._q = queue.Queue()
        self._buf = np.zeros(0, dtype=np.float32)
        self._last_fire = 0.0
        self._suspend_until = 0.0
        self._last_browser_wake = 0.0
        self._last_decode = 0.0
        self._hold_until = 0.0
        self._paused = False
        self._last_audio_ts = 0.0
        self._running = False
        self._thread = None
        self._stream = None
        self._callback = None  # called when wake detected: callback()
        self._on_error = None  # called on fatal error: on_error(msg)
        self.healthy = False
        self.last_err = ''
        self._last_err_print = 0.0  # throttles transcribe-error prints to 1/min
        self._last_reopen = 0.0
        self._gate = None      # why the listener is not decoding; None = decoding
        self._log = self._write_debug
        self._speaking_until = 0.0
        self._spoken = deque(maxlen=8)   # (time, text) of what Cygnus said lately

    # -- external signals ---------------------------------------------------
    def set_callback(self, cb):
        self._callback = cb

    def set_error_callback(self, cb):
        self._on_error = cb

    def suspend(self, ms=None):
        """Mute the listener briefly (e.g. right after JARVIS speaks TTS)."""
        # self.suspend_ms never existed — the ctor stores suspend_s — so every
        # no-arg suspend() raised AttributeError. All three callers wrap this in
        # a bare except, so the failure was silent and the engine kept decoding
        # straight through JARVIS's own TTS.
        s = (ms / 1000.0) if ms else self.suspend_s
        self._suspend_until = max(self._suspend_until, time.time() + s)

    def pause(self):
        """Hold the listener off until resume(): a command turn is in flight.

        Unlike suspend(), this has no deadline — a turn can run for a minute and
        we must not resume decoding halfway through it and steal its cores.
        """
        self._paused = True

    def resume(self):
        self._paused = False

    def hold(self, ms):
        """The browser is recording a command: it owns the mic, so stay quiet.
        A deadline, not a flag, so a capture that never reports back can't
        leave the fallback deaf. Separate from suspend(): release() must not
        cut short the post-TTS window."""
        self._hold_until = time.time() + ms / 1000.0

    def release(self):
        self._hold_until = 0.0

    def note_browser_wake(self):
        """The browser's own Web Speech listener fired — don't double-trigger."""
        self._last_browser_wake = time.time()

    def speaking(self, on):
        """The HUD started or stopped playing Cygnus's voice."""
        self._speaking_until = time.time() + SPEAKING_CAP_S if on else 0.0
        if not on:
            self.suspend()        # the last syllables still ring in the room

    def note_spoken(self, text):
        """A line Cygnus is about to say (every TTS passes through here)."""
        if text:
            self._spoken.append((time.time(), text))

    def _own_voice(self, now):
        """A wake heard now is Cygnus's own voice: it is talking, and something
        it just said holds the wake word. Otherwise a wake while it talks is
        the owner cutting in, and counts."""
        return now < self._speaking_until and any(
            now - ts < ECHO_WINDOW_S and wake_word_in(said) for ts, said in self._spoken)

    def note(self, msg):
        """One line in the wake debug log (the server adds what the HUD did)."""
        self._log(msg)

    def _write_debug(self, msg):
        if not WAKE_DEBUG:
            return
        try:
            _debug_logger().info(msg)
        except Exception:
            pass

    def _set_gate(self, gate):
        """Log the reason the listener is not decoding, once per change."""
        if gate != self._gate:
            self._gate = gate
            self._log(f"listener: {gate or 'decoding'}")

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        if self._running:
            return
        try:
            self._open_stream()
            self._running = True
            self._last_audio_ts = time.time()
            self.healthy = True
            self.last_err = ''
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            print("[WakeEngine] started (server-side wake fallback)", flush=True)
        except Exception as e:
            self.healthy = False
            self.last_err = str(e)
            print(f"[WakeEngine] FAILED to start: {e}", flush=True)
            if self._on_error:
                self._on_error(str(e))

    def stop(self):
        self._running = False
        self._close_stream()

    def _open_stream(self):
        self._stream = sd.InputStream(
            device=self.device, samplerate=self.sr, channels=1,
            dtype='float32', blocksize=int(self.sr * 0.1),
            callback=self._audio_cb)
        self._stream.start()

    def _close_stream(self):
        try:
            if self._stream:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None

    def _maybe_reopen(self, now):
        """A stream that stopped delivering audio died (sleep, a headset
        unplugged, the device taken). It used to be marked unhealthy and left
        that way until a restart; reopen it instead, at most every
        REOPEN_EVERY_S, so the wake word comes back on its own."""
        if now - self._last_audio_ts <= SILENT_STREAM_S:
            return
        self.healthy = False
        self.last_err = 'no microphone audio received'
        if now - self._last_reopen < REOPEN_EVERY_S:
            return
        self._last_reopen = now
        self._close_stream()
        try:
            self._open_stream()
            print("[WakeEngine] microphone stream reopened", flush=True)
        except Exception as e:
            self.last_err = f'microphone unavailable: {e}'

    def _audio_cb(self, indata, frames, time_info, status):
        try:
            self._q.put(np.copy(indata[:, 0]))
            self._last_audio_ts = time.time()
        except Exception:
            pass

    # -- capture + detect loop ---------------------------------------------
    def _loop(self):
        while self._running:
            try:
                chunks = [self._q.get(timeout=1.0)]
            except queue.Empty:
                self._maybe_reopen(time.time())
                continue
            # Everything that arrived during the last decode, at once: only the
            # newest window matters, and one block per pass would fall behind.
            while True:
                try:
                    chunks.append(self._q.get_nowait())
                except queue.Empty:
                    break
            self.healthy = True
            self.last_err = ''
            self._tick(chunks, time.time())

    def _tick(self, chunks, now):
        """Buffer new audio; decode the newest window when every gate allows.
        Returns True when it ran Whisper."""
        step_samples = int(self.sr * self.step_s)
        window_samples = int(self.sr * self.window_s)
        self._buf = np.concatenate([self._buf] + list(chunks))
        # Keep at most one window + a step of history.
        cap = window_samples + step_samples
        if self._buf.shape[0] > cap:
            self._buf = self._buf[-cap:]

        gate = ('cooldown after a wake' if now - self._last_fire < self.cooldown else
                'paused: a command turn is running' if self._paused else
                'suspended: Cygnus is speaking' if now < self._suspend_until else
                'held: the HUD is recording a command' if now < self._hold_until else
                'browser handled the wake' if now - self._last_browser_wake < 2.0 else None)
        self._set_gate(gate)
        if gate or self._buf.shape[0] < window_samples:
            return False
        if now - self._last_decode < self.step_s:
            return False                      # at most one decode per step

        window = self._buf[-window_samples:].astype(np.float32)
        # Skip near-silent windows (room tone) — no point burning Whisper.
        # 0.1 s block means, then a BURST_S moving average: the loudest slice.
        blk = int(self.sr * 0.1)
        n = window.shape[0] // blk
        means = np.abs(window[:n * blk]).reshape(n, blk).mean(axis=1)
        k = max(1, min(n, int(round(BURST_S / 0.1))))
        level = np.convolve(means, np.ones(k) / k, mode='valid').max()
        if level < SILENCE_FLOOR:
            return False
        self._last_decode = now
        try:
            if self.spotter is not None:
                text = self.spotter.hears(window, self.sr)
            else:
                # Cheap wake-only model, not the command model — see
                # VoiceEngine.transcribe_wake() and WAKE_WHISPER_MODEL.
                text = self.ve.transcribe_wake(window)
        except Exception as e:
            self.last_err = f'transcribe error: {e}'
            if now - self._last_err_print >= 60.0:
                self._last_err_print = now
                print(f"[WakeEngine] transcribe error: {e!r}", flush=True)
            return True
        hit = wake_word_in(text)
        echo = hit and self._own_voice(now)
        via = f" via {self.spotter.name}" if self.spotter is not None else ""
        self._log(f"heard {text!r} (level {level:.4f}){via}"
                  + (" -> ignored: its own reply says the wake word" if echo else " -> WAKE" if hit else ""))
        if hit and not echo:
            print(f"[WakeEngine] wake word detected: {text!r}", flush=True)
            delivered = True
            if self._callback:
                try:
                    # A callback returning False means nobody got the wake;
                    # None (no return value) counts as delivered.
                    delivered = self._callback() is not False
                except Exception as e:
                    delivered = False
                    print(f"[WakeEngine] callback error: {e!r}", flush=True)
            self._log("wake sent to the HUD" if delivered else "wake NOT delivered: no HUD connected")
            if delivered:
                self._last_fire = now
        return True
