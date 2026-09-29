#!/usr/bin/env python3
"""
JARVIS Web Interface - FastAPI backend for voice visual JARVIS

WebSocket endpoint for real-time STT/TTS loop with animated orb visualization.
Browser captures mic -> sends audio (WebM/Opus) -> backend transcribes with Whisper
-> Gemini thinks -> edge-tts speaks -> audio sent back to browser.
"""

import os
import io
import sys
import json
from cygnus import paths

# The console (and a redirected log) is cp1252 on Windows: a page read or a
# job note with "↘" or "ロ" crashed the print, and with it the announcement.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
import time
import base64
import tempfile
import asyncio
import threading

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

from cygnus.voice.engine import VoiceEngine
from cygnus.voice.wake import WakeEngine, after_wake, wake_targets
from cygnus.brain.core import JarvisBrain
from cygnus.memory.session_store import log as sstore_log
from cygnus.brain import intake
from cygnus.brain.intake import load_corpus, resolve_intent, with_names

# Layer 1 (Intake) corpus — portable data, reloaded once at boot.
INTAKE_CORPUS = load_corpus()
from cygnus.config import (GEMINI_MODEL, ROUTER_BASE_URL, ROUTER_MODEL, WHISPER_MODEL, GROQ_MODEL, GROQ_BASE_URL, CEREBRAS_MODEL, CEREBRAS_BASE_URL)

app = FastAPI(title="Cygnus Voice Interface")


# Browser origins allowed besides JARVIS's own (the phone's Tailscale https
# name). Comma-separated in .env.
_ALLOWED_ORIGINS = frozenset(
    o.strip().rstrip("/") for o in os.environ.get("JARVIS_ALLOWED_ORIGINS", "").split(",")
    if o.strip())


def _hostname(host_header: str) -> str:
    """A Host header without its port; IPv6 keeps its brackets."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


# Names this server answers to. A DNS-rebinding page (evil.example pointed at
# 127.0.0.1) arrives with its own name in Host, and its Origin matches that
# Host, so the origin checks alone would let it in.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"} |
                           {_hostname(o.split("://", 1)[-1]) for o in _ALLOWED_ORIGINS})


@app.middleware("http")
async def _guard_apps_writes(request: Request, call_next):
    """Every request must name an allowed host. Apps-panel writes (rules,
    toggles, re-checks) come only from JARVIS's own pages: the server has no
    login and the handlers parse the body as JSON whatever its type, so
    without this any web page the user visits could post a rule (a
    text/plain POST skips the CORS preflight)."""
    if _hostname(request.headers.get("host", "")) not in _ALLOWED_HOSTS:
        return JSONResponse({"error": "unknown host"}, status_code=400)
    # What you said (shadow review) and your learned defaults stay on this
    # computer. Requests proxied by tailscale serve also come from 127.0.0.1,
    # so the phone passes this check; it is your own device.
    if request.url.path.startswith(("/shadow", "/prefs")) and \
            (request.client.host if request.client else "") not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"error": "Only available on this computer."}, status_code=403)
    if request.method in ("POST", "PUT", "PATCH", "DELETE") \
            and request.url.path.startswith(("/apps", "/shadow", "/prefs")):
        ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        origin = request.headers.get("origin")
        same_origin = not origin or \
            origin.split("://", 1)[-1].rstrip("/") == request.headers.get("host", "")
        if ctype != "application/json" or not same_origin:
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)

# Connected voice clients (JARVIS HUD tabs) — the server-side wake engine alerts
# the local ones (see wake_targets) so a wake detected on the server opens the
# command window in the UI.
WS_CLIENTS = set()
# The subset that came in through the tailscale proxy (the phone). The PC's
# wake engine hears the PC's room, so its wakes must not open a capture on a
# phone somewhere else; those clients are tap-to-talk.
WS_REMOTE = set()
# The desktop app's window (it says hello on connect). While one is
# connected, server wakes go to it alone, so a Chrome tab stays quiet.
WS_DESKTOP = set()
# "Go full HUD" said from anywhere moves the desktop window (window_view.py);
# with no window open, Cygnus says so instead of promising.
from cygnus.brain import window_view
window_view.desktop_open = lambda: bool(WS_DESKTOP)


async def _send_view(view) -> None:
    """After a "view" turn's reply: the desktop window switches itself."""
    if view not in window_view.VIEWS:
        return
    payload = json.dumps({"type": "view", "view": view})
    for ws in list(WS_DESKTOP):
        try:
            await ws.send_text(payload)
        except Exception as e:
            print(f"[WS] view {view} not sent: {e}", flush=True)

# JARVIS_NO_VOICE=1: no Whisper, no microphone, no speech - typed turns only.
# For a machine with no microphone, a headless run, and the benchmark's second
# instance, which must not take the mic or load Whisper twice.
NO_VOICE = os.environ.get("JARVIS_NO_VOICE", "0").strip() == "1"


class _NoVoice:
    """Stands in for the voice and wake engines when NO_VOICE is set: every
    call does nothing, and nothing can be transcribed."""

    def transcribe_with_hint(self, *a, **k):
        return "", "none", "", 0.0

    def _transcribe_from_file(self, *a, **k):
        return ""

    def __getattr__(self, _name):
        return lambda *a, **k: None


# Global singletons (Whisper load is slow, do it once)
if NO_VOICE:
    print("[JARVIS Web] JARVIS_NO_VOICE=1: no Whisper, no microphone, typed turns only.", flush=True)
    voice_engine = _NoVoice()
else:
    print("[JARVIS Web] Loading VoiceEngine (Whisper)...", flush=True)
    voice_engine = VoiceEngine()
print(f"[JARVIS Web] Loading Brain ({ROUTER_MODEL})...", flush=True)
brain = JarvisBrain()
# The semantic router loads in the background (~5 s); until then it neither
# logs nor leads, and no reply waits for it.
from cygnus.brain import semantic_route
semantic_route.warm()
# 9router health: probe at boot, so the first cloud turn already skips dead
# models (router_health.py); it re-checks every 5 minutes after that.
from cygnus.brain import router_health
router_health.start()
# ...and 9router itself: started when it is down, checked every 30 s, since
# only the login script and the desktop app ever started it (router_keeper.py).
from cygnus.brain import router_keeper
router_keeper.watch()
from cygnus import tools  # for the /apps/open endpoint (same dispatcher the brain uses)

# Server-side wake-word fallback. The browser's Web Speech WakeListener is primary,
# but it degrades when music/ambient noise bleeds into the mic — this catches the
# wake word with Whisper (verified robust to music) and tells the HUD to open its
# command window. Healthy=false just means "no server-side fallback"; the browser
# listener still works on its own.
# Post-TTS suspend window, tunable without code edits (JARVIS_WAKE_SUSPEND_MS).
try:
    _WAKE_SUSPEND_MS = max(0, int(os.environ.get("JARVIS_WAKE_SUSPEND_MS", "1500")))
except ValueError:
    print("[WakeEngine] bad JARVIS_WAKE_SUSPEND_MS, using 1500", flush=True)
    _WAKE_SUSPEND_MS = 1500
_wake_spotter = None
try:
    from cygnus.config import JARVIS_WAKE_ENGINE
    if JARVIS_WAKE_ENGINE == "sherpa" and not NO_VOICE:
        from cygnus.voice import wake_kws
        _wake_spotter = wake_kws.load()
except Exception as _e:
    print(f"[WakeEngine] spotter setup failed ({_e}); using Whisper", flush=True)
wake_engine = _NoVoice() if NO_VOICE else WakeEngine(
    voice_engine, suspend_ms=_WAKE_SUSPEND_MS, spotter=_wake_spotter)
# Longest the wake fallback stays quiet for one browser capture.
_WAKE_HOLD_MS = 30000

# The server's event loop, captured by websocket_endpoint. broadcast_wake runs on
# the WakeEngine worker thread, which has no loop of its own — asyncio.get_event_loop()
# there never schedules the send, so every server wake reached nobody.
_WS_LOOP = None
_WS_LOOP_WARN_TS = 0.0


def _wake_send_done(ws, fut):
    """Runs on the server loop once a wake send finishes; drops only sockets that failed."""
    if fut.cancelled():
        exc = asyncio.CancelledError()
    else:
        exc = fut.exception()
    if exc is not None:
        print(f"[WakeEngine] wake send failed, dropping client: {exc!r}", flush=True)
        WS_CLIENTS.discard(ws)


def broadcast_wake():
    """Called by the server wake engine when it hears 'jarvis'.

    Returns True when at least one send was scheduled, so the engine only starts
    its cooldown on a wake that actually went somewhere.
    """
    global _WS_LOOP_WARN_TS
    loop = _WS_LOOP
    if loop is None or not loop.is_running():
        now = time.time()
        if now - _WS_LOOP_WARN_TS >= 60.0:
            _WS_LOOP_WARN_TS = now
            print("[WakeEngine] wake heard but no server loop yet (no HUD connected); "
                  "clients kept", flush=True)
        return False
    clients = wake_targets(WS_CLIENTS, WS_REMOTE, WS_DESKTOP)
    target = "desktop app" if WS_DESKTOP.intersection(clients) else "local"
    print(f"[WakeEngine] broadcasting wake to {len(clients)} HUD client(s) "
          f"({target}; {len(WS_REMOTE)} remote skipped)", flush=True)
    payload = json.dumps({"type": "wake", "source": "server"})
    scheduled = 0
    for ws in clients:
        coro = ws.send_text(payload)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            coro.close()  # never scheduled — close it so it isn't reported unawaited
            print(f"[WakeEngine] wake schedule failed, dropping client: {e!r}", flush=True)
            WS_CLIENTS.discard(ws)
            continue
        fut.add_done_callback(lambda f, ws=ws: _wake_send_done(ws, f))
        scheduled += 1
    return scheduled > 0


if not NO_VOICE:
    wake_engine.set_callback(broadcast_wake)
    wake_engine.set_error_callback(
        lambda msg: print(f"[WakeEngine] error: {msg}", flush=True))
    wake_engine.start()

# The SERVER process runs Cygnus's jobs, so it alone may call a job the log
# still shows running "interrupted by restart" (jobs.claim_ownership). That
# claim is made in __main__, not here: importing this module from a test
# (apps_panel_test.py, a delegated Claude's pytest run) claimed the registry
# and marked the very job that ran the test interrupted (2026-09-15).

# Phase 2: pre-warm Hermes session at boot so the first voice query doesn't
# pay the full cold-start cost. Runs in a background thread to avoid blocking
# server startup. The session is persisted on disk and survives JARVIS restarts.
def _prewarm_hermes():
    try:
        from cygnus.jobs.warm_harness import warm_session
        sid = warm_session(timeout=120)
        if sid:
            print(f"[JARVIS Web] Hermes warm session ready: {sid}", flush=True)
        else:
            print("[JARVIS Web] Hermes warm session: no session_id (will spawn fresh)", flush=True)
    except Exception as e:
        print(f"[JARVIS Web] Hermes warm-up failed: {e}", flush=True)

# Not under pytest: importing this module from a test started a real Hermes
# session in a thread, which then overwrote another test's monkeypatched
# session id and failed it at random (2026-09-23, test_warm_fresh).
if "PYTEST_CURRENT_TEST" not in os.environ:
    threading.Thread(target=_prewarm_hermes, daemon=True).start()

# Phase 8: refresh the web registry in the background ONLY if older than 7
# days (policy C). One stat call when fresh; voice resolution uses the current
# registry immediately either way.
try:
    from cygnus.config import SCAN_BROWSER_HISTORY
    if not SCAN_BROWSER_HISTORY:
        print("[JARVIS Web] Browser-history scan off (JARVIS_SCAN_BROWSER_HISTORY=1 turns it on); "
              "the Apps panel can still rescan on request.", flush=True)
    else:
        from cygnus.apps import web_registry as _wr
        if _wr.maybe_refresh_async():
            print("[JARVIS Web] Web registry stale — background rescan started.", flush=True)
except Exception as _e:
    print(f"[JARVIS Web] Web registry check failed: {_e}", flush=True)

print("[JARVIS Web] Ready.", flush=True)

# Voice state feedback (Phase A): policy engine + cue cache. All spoken state
# updates (thinking cues, progress lines, terminal job announcements) route
# through voice_feedback.announce() so the user gets friendly feedback without
# overlapping/duplicate speech. The synthesizer is registered after tts_to_b64
# is defined below.
from cygnus.voice import feedback as _vf
from cygnus.voice import ducking as voice_ducking

# Phase B: per-job milestone timers ("Still on it, sir.") for long background
# tasks — keyed by job id, cancelled when the job reaches a terminal state.
_TASK_MILESTONES: dict[str, asyncio.Task] = {}


class _ThinkingWatchdog:
    """Speaks 'Thinking, sir.' / 'Still working on it.' if a turn runs long."""

    def __init__(self, ws, vf):
        self._ws = ws
        self._vf = vf
        self._stop = asyncio.Event()

    def start(self):
        asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        try:
            await asyncio.sleep(_vf.THINKING_CUE_S)
            if self._stop.is_set():
                return
            await _send_cue(self._ws, "thinking", self._vf)
            await asyncio.sleep(max(1.0, _vf.THINKING_ESCALATE_S - _vf.THINKING_CUE_S))
            if self._stop.is_set():
                return
            await _send_cue(self._ws, "thinking_escalate", self._vf)
        except Exception:
            pass

    def stop(self):
        self._stop.set()


async def _send_cue(ws, kind: str, vf) -> None:
    line = vf.announce(kind)
    if line is None:
        return
    audio = await vf.cue_audio(line)
    try:
        await ws.send_text(json.dumps({
            "type": "cue", "text": line, "audio": audio, "priority": "low" if kind.startswith("thinking") else "normal"
        }))
    except Exception:
        pass


def _start_confirm_reminder(ws, vf) -> None:
    """Phase B: if a confirm gate is still pending after 5 min, remind once."""
    async def _run():
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            return
        try:
            from cygnus.tools import _PENDING_HERMES_CALL, _PENDING_DESTRUCTIVE
            pending = _PENDING_HERMES_CALL or _PENDING_DESTRUCTIVE.get("autonomous")
        except Exception:
            return
        if pending:
            await _send_cue(ws, "confirm_pending", vf)
    asyncio.get_running_loop().create_task(_run())


def _start_task_milestones(ws, vf, jid: str) -> None:
    """Phase B: speak a friendly 'still on it' line every 45s (max 3) while a
    background job is genuinely still running; cancelled on its terminal event."""
    _TASK_MILESTONES.pop(jid, None)  # cancel any stale timer for this job

    async def _run():
        lines = ["Still on it, sir.", "Making progress, sir.",
                 "This one is taking a while, sir."]
        try:
            for line in lines:
                await asyncio.sleep(45)
                from cygnus.jobs import registry as _jobs
                j = _jobs.get(jid)
                if not j or j.get("state") not in ("running", "queued"):
                    return
                spoken = vf.announce("milestone", line)
                if spoken:
                    audio = await vf.cue_audio(spoken)
                    await ws.send_text(json.dumps({
                        "type": "cue", "text": spoken, "audio": audio,
                        "priority": "normal"}))
        except asyncio.CancelledError:
            return
        except Exception:
            pass

    _TASK_MILESTONES[jid] = asyncio.get_running_loop().create_task(_run())


def _arm_phase_b(ws, vf, response_text) -> None:
    """Arm the per-turn Phase B timers from the raw think() response."""
    if not isinstance(response_text, str):
        return
    if ("HERMES_BACKGROUND:" in response_text
            or "AUTONOMOUS_BACKGROUND:" in response_text):
        ack = response_text.split(":", 1)[1].strip()
        m = _re.search(r"\(job ([0-9a-f]+)\)", ack)
        if m:
            _start_task_milestones(ws, vf, m.group(1))
    elif response_text.startswith("[NEEDS_CONFIRM"):
        _start_confirm_reminder(ws, vf)
    elif response_text.startswith("[NEEDS_PICK]"):
        # An ambiguous app name was NOT launched; JARVIS asked which one. Same
        # reminder treatment as a confirm — the turn is waiting on the user.
        _start_confirm_reminder(ws, vf)


def decode_audio_to_wav(audio_bytes: bytes) -> str:
    """
    Decode arbitrary browser audio (WebM/Opus, OGG/Opus, MP3, WAV...) to a
    16kHz mono WAV file on disk. Returns the temp .wav path.
    Uses PyAV to decode, soundfile/whisper expect WAV PCM.
    """
    import av
    import soundfile as sf
    import numpy as np

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name

    try:
        # Decode with PyAV
        container = av.open(io.BytesIO(audio_bytes))
        # Find best audio stream
        audio_stream = None
        for s in container.streams:
            if s.type == "audio":
                audio_stream = s
                break
        if audio_stream is None:
            raise ValueError("No audio stream found in uploaded data")

        resampler = av.audio.resampler.AudioResampler(
            format="s16",
            layout="mono",
            rate=16000,
        )

        frames = []
        for frame in container.decode(audio_stream):
            for resampled in resampler.resample(frame):
                # resampled.frame is planar s16 -> convert to numpy
                import numpy as np
                arr = resampled.to_ndarray()
                # shape (channels, samples) -> flatten mono already
                if arr.ndim > 1:
                    arr = arr[0]
                frames.append(arr.astype(np.float32) / 32768.0)

        if not frames:
            raise ValueError("Decoded audio is empty")

        audio_np = np.concatenate(frames, axis=0).astype(np.float32)

        sf.write(tmp_wav, audio_np, 16000, subtype="PCM_16")
        return tmp_wav

    except Exception as e:
        # Fallback: maybe it's already a WAV
        try:
            import soundfile as sf
            sf.read(io.BytesIO(audio_bytes))
            # It's a valid wav, just write through
            with open(tmp_wav, "wb") as f:
                f.write(audio_bytes)
            return tmp_wav
        except Exception:
            raise RuntimeError(f"Could not decode audio: {e}")


import re as _re

# Speech: the reply's last cleanup and the TTS engines (server/speech.py).
from cygnus.server.speech import (  # noqa: E402,F401
    _duck_failsafe_loop,
    _get_kokoro,
    _get_piper,
    _KOKORO_DIR,
    _KOKORO_TTS,
    _PIPER_MODEL,
    _piper_voice,
    _TTS_ENGINE,
    strip_ai_artifacts,
    tts_to_b64,
)

# Rolling counters so the HUD can show a backend mix and a fallback rate rather
# than a decorative gauge. Kept in memory only: this is a demo readout, not
# metrics anyone should depend on.
TURN_STATS = {"turns": 0, "by_backend": {}, "latencies": []}

# Model facts, read once from Ollama. Static for the process, so there is no
# reason to pay for it on every turn.
MODEL_META = {}


def _load_model_meta():
    """Size/params/quant of the local model, straight from Ollama."""
    try:
        import urllib.request
        from cygnus.config import OLLAMA_MODEL
        req = urllib.request.Request(
            "http://localhost:11434/api/show",
            data=json.dumps({"name": OLLAMA_MODEL}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
        det = d.get("details", {}) or {}
        MODEL_META.update({
            "params": det.get("parameter_size"),
            "quant": det.get("quantization_level"),
            "family": det.get("family"),
        })
        for line in (d.get("parameters") or "").splitlines():
            if line.strip().startswith("num_ctx"):
                MODEL_META["num_ctx"] = int(line.split()[-1])
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=10) as r:
                for m in json.load(r).get("models", []):
                    if m.get("name", "").startswith(OLLAMA_MODEL):
                        MODEL_META["size_gb"] = round(m.get("size", 0) / 1e9, 2)
                        break
        except Exception:
            pass
    except Exception as e:
        print(f"[JARVIS Web] model meta unavailable ({e})", flush=True)


_load_model_meta()


def _log_turn_timing(t):
    """One line per voice turn: where the wall clock actually went.

    Stages are cumulative timestamps; we print the deltas so a slow turn names
    its own culprit instead of needing a hand-run benchmark.
    """
    try:
        order = [("decode", "start"), ("stt", "decode"),
                 ("brain", "stt"), ("tts", "brain")]
        parts = []
        for name, prev in order:
            if name in t and prev in t:
                parts.append(f"{name} {t[name] - t[prev]:.2f}s")
        last = t.get("tts") or t.get("brain") or t.get("stt")
        total = f" | total {last - t['start']:.2f}s" if last else ""
        print(f"[TIMING] {' | '.join(parts)}{total}", flush=True)
    except Exception:
        pass  # instrumentation must never break a turn


async def send_telemetry(websocket: WebSocket):
    """Push the per-turn numbers the HUD panels read.

    Only what the backend actually reported: a field the brain did not record is
    simply absent, so the HUD leaves that row blank instead of showing a made-up
    figure next to real ones.
    """
    try:
        stats = dict(getattr(brain, "last_stats", {}) or {})
        backend = stats.get("backend") or brain.last_backend
        if backend:
            TURN_STATS["turns"] += 1
            TURN_STATS["by_backend"][backend] = TURN_STATS["by_backend"].get(backend, 0) + 1
        if stats.get("latency_ms"):
            TURN_STATS["latencies"].append(stats["latency_ms"])
            del TURN_STATS["latencies"][:-10]          # last 10 turns only

        lat = sorted(TURN_STATS["latencies"])
        stats["p50_ms"] = lat[len(lat) // 2] if lat else None
        stats["turns"] = TURN_STATS["turns"]
        stats["mix"] = TURN_STATS["by_backend"]
        # Real window from the model itself rather than a hardcoded 8192.
        stats["context_limit"] = MODEL_META.get("num_ctx") if backend == "ollama" else None
        stats["history_msgs"] = len(getattr(brain, "conversation", []) or [])
        stats["model_meta"] = MODEL_META
        stats["local_only"] = bool(getattr(brain, "_local_only", False))
        await websocket.send_text(json.dumps({"type": "telemetry", "stats": stats}))
    except Exception as e:
        print(f"[WS] telemetry error: {e}", flush=True)


def _known_names():
    """Lowercase names of the user's pinned apps and registered sites, for
    the transcript picker: the transcript that names one is the one meant."""
    names = set()
    try:
        from cygnus.apps import abilities as app_abilities
        from cygnus.apps import curate
        apps = app_abilities.load_apps()["apps"]
        for key in curate.registered_apps():
            entry = apps.get(key) or {}
            for n in (key, entry.get("name"), entry.get("display_name"),
                      app_abilities.DISPLAY_NAMES.get(key)):
                if n:
                    names.add(" ".join(n.lower().split()))
    except Exception:
        pass
    try:
        from cygnus.apps import web_registry
        for key, site in (web_registry.load_registry().get("sites") or {}).items():
            names.add(key.lower())
            names.update(a.lower() for a in site.get("aliases") or [])
    except Exception:
        pass
    return {n for n in names if len(n) >= 3}


def _job_cards(recent_s: float = 900) -> list[dict]:
    """Active jobs and jobs that ended in the last `recent_s` seconds, shaped
    like job events plus their plain card fields (plain_reply.card)."""
    from cygnus.jobs import registry as jobs
    from cygnus.brain import plain_reply
    now = time.time()
    rows, seen = [], set()
    for j in jobs.active() + jobs.recent(8):
        ended = (j.get("started") or 0) + (j.get("elapsed") or 0)
        if j["id"] in seen or (j.get("state") not in ("queued", "running", "waiting-on-confirm")
                               and now - ended > recent_s):
            continue
        seen.add(j["id"])
        ev = {"type": "job", "id": j["id"], "task": j.get("task", ""), "state": j.get("state"),
              "note": (j.get("progress") or [None])[-1], "error": j.get("error"),
              "summary": j.get("summary"), "elapsed": j.get("elapsed")}
        rows.append({**ev, **plain_reply.card(ev)})
    return rows


# Replies that could not be sent because the client's socket closed mid-turn
# (a phone reconnecting, 2026-09-12: "websocket.send after websocket.close"),
# kept per client and sent when that client connects again. Last 3, 10 min.
_UNDELIVERED: dict[str, list] = {}     # client -> [(time, payload)]
_UNDELIVERED_TTL_S = 600
# The socket each client has right now ("remote", "local", "desktop",
# "session:<id>"): a reply for a client goes to its current socket, not to
# the one its request came in on. The phone's browser drops the socket
# whenever the screen dims, and on 2026-09-15 two slash commands were
# cancelled with the connection they arrived on: nothing ran, nothing said.
_SOCKETS: dict[str, WebSocket] = {}


def _stash(client: str | None, payload: dict) -> None:
    if not client or not payload:
        return
    rows = [r for r in _UNDELIVERED.get(client, []) if time.time() - r[0] < _UNDELIVERED_TTL_S]
    _UNDELIVERED[client] = (rows + [(time.time(), payload)])[-5:]
    print(f"[WS] {payload.get('type')} kept for {client} (no socket): "
          f"{(payload.get('text') or payload.get('target') or '')[:60]!r}", flush=True)


def _stash_reply(client: str | None, text: str) -> None:
    if text:
        _stash(client, {"type": "response", "text": strip_ai_artifacts(text), "audio": None, "deferred": True})


async def _flush_undelivered(websocket: WebSocket, client: str) -> None:
    rows = [r for r in _UNDELIVERED.pop(client, []) if time.time() - r[0] < _UNDELIVERED_TTL_S]
    for _ts, payload in rows:
        out = dict(payload)
        if out.get("type") == "response":
            out["text"] = "While you were reconnecting: " + (out.get("text") or "")
        elif out.get("type") == "app_plan" and out.get("id") not in _APP_PLANS:
            continue                                # the plan expired meanwhile
        try:
            await websocket.send_text(json.dumps(out))
        except Exception:
            _UNDELIVERED.setdefault(client, []).append((_ts, payload))
            return


async def _deliver(client: str, payload: dict) -> bool:
    """Send to the client's current socket; keep it for the next one if there
    is none or it fails. Statuses are dropped rather than kept."""
    ws = _SOCKETS.get(client)
    if ws is not None:
        try:
            await ws.send_text(json.dumps(payload))
            return True
        except Exception:
            pass
    if payload.get("type") != "status":
        _stash(client, payload)
    return False


async def deliver_result(websocket: WebSocket, text: str, speak: bool, client: str | None = None):
    """Phase 4: deliver a deferred Hermes answer (called from the background thread
    via run_coroutine_threadsafe once Hermes returns). Sends it as a fresh
    `response` event with TTS, so the voice client hears the result whenever it
    lands — without having blocked the mic meanwhile."""
    try:
        await websocket.send_text(json.dumps({"type": "status", "state": "speaking"}))
        tts_b64 = await tts_to_b64(text) if speak else None
        text = strip_ai_artifacts(text)  # clean displayed text too, not just audio
        # JARVIS is about to talk — mute the server wake listener briefly so it
        # doesn't hear its own voice and re-trigger a capture. (Music is not TTS,
        # so this does NOT mute it during user speech — the whole point of the
        # fallback is to survive music.)
        try:
            wake_engine.suspend()
        except Exception:
            pass
        await websocket.send_text(json.dumps({
            "type": "response", "text": text, "audio": tts_b64, "deferred": True
        }))
        await send_telemetry(websocket)
        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
    except Exception as e:
        print(f"[WS] deliver_result error: {e}", flush=True)
        _stash_reply(client, text)



def _no_cache(resp):
    """HUD pages must never be served from browser cache — a stale page makes
    the user see an old HUD after every redesign (bit us 2026-08-28)."""
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/")
async def get():
    html_path = str(paths.HUD_HTML)
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


@app.get("/manifest.webmanifest")
async def get_manifest():
    """Lets Android Chrome install the HUD to the home screen (standalone,
    no browser bar). Icons are served below."""
    return JSONResponse({
        "name": "Cygnus", "short_name": "Cygnus", "start_url": "/",
        "display": "standalone", "background_color": "#05070c",
        "theme_color": "#05070c",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
                   "purpose": "any maskable"},
                  {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
                   "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


@app.get("/icon-{size}.png")
async def get_icon(size: int):
    from fastapi.responses import FileResponse
    path = str(paths.STATIC_DIR / f"icon-{size}.png")
    if not os.path.exists(path):
        return JSONResponse({"error": "no such icon"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/vendor/{name}")
async def get_vendor_asset(name: str):
    """Third-party front-end files, served from web/vendor/ instead of a CDN.

    The HUD used to load lucide from unpkg at @latest, unpinned and without
    SRI: whatever that URL served that day ran inside a page holding a live
    socket to an assistant that can act on the machine. It also told unpkg the
    IP of everyone who opened the HUD. Vendored, so neither is true, and the
    HUD works with no internet at all.
    """
    import re
    from fastapi.responses import FileResponse
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(js|css)", name):
        return JSONResponse({"error": "no such asset"}, status_code=404)
    path = paths.WEB_DIR / "vendor" / name
    if not path.is_file():
        return JSONResponse({"error": "no such asset"}, status_code=404)
    return FileResponse(str(path),
                        media_type="application/javascript" if name.endswith(".js") else "text/css")


@app.get("/hud.html")
async def get_hud():
    # The untouched HUD artifact (served into an iframe)
    html_path = str(paths.HUD_ARTIFACT_HTML)
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


# The Apps panel's endpoints (server/apps_routes.py), included here so the
# route order is what it was.
from cygnus.server import apps_routes as _apps_routes  # noqa: E402
from cygnus.server.apps_routes import (  # noqa: E402,F401
    _apps_job,
    _capability_items,
    _json_body,
    _known_app,
    _website_items,
    DELEGATE_REGISTRY_PATH,
    get_apps,
    get_apps_panel,
    get_apps_registry,
    get_apps_rules_list,
    get_apps_rules_progress,
    get_apps_scan_all,
    get_prefs,
    get_shadow_review,
    post_apps_abilities_generate,
    post_apps_abilities_scan,
    post_apps_abilities_test,
    post_apps_abilities_update,
    post_apps_add,
    post_apps_compile,
    post_apps_fullscan,
    post_apps_hide,
    post_apps_open,
    post_apps_register,
    post_apps_rules,
    post_apps_rules_begin,
    post_apps_rules_clarify,
    post_apps_rules_clear,
    post_apps_rules_recheck,
    post_apps_rules_test,
    post_apps_rules_test_answer,
    post_apps_scan_all,
    post_apps_scan_all_cancel,
    post_apps_toggle,
    post_prefs_forget,
    post_shadow_label,
)
app.include_router(_apps_routes.router)


def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """Browsers skip CORS for WebSockets, so without this any web page open
    on a device that can reach the server could connect to /ws and drive the
    tools. The Host must be one of _ALLOWED_HOSTS (HTTP middleware does not
    run for WebSockets). Then accept JARVIS's own pages (Origin matches
    Host), the JARVIS_ALLOWED_ORIGINS list, and non-browser clients that
    send no Origin (test clients)."""
    host = websocket.headers.get("host", "")
    if _hostname(host) not in _ALLOWED_HOSTS:
        return False
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    if origin.split("://", 1)[-1].rstrip("/") == host:
        return True
    return origin.rstrip("/") in _ALLOWED_ORIGINS


def _ws_is_remote(websocket: WebSocket) -> bool:
    """A proxied client (tailscale serve adds X-Forwarded-For) or one that
    addressed the server by anything but its loopback name is not in this
    room."""
    if websocket.headers.get("x-forwarded-for"):
        return True
    return _hostname(websocket.headers.get("host", "")) not in ("127.0.0.1", "localhost", "[::1]")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global _WS_LOOP
    if not _ws_origin_allowed(websocket):
        print(f"[WS] rejected origin {websocket.headers.get('origin')!r}", flush=True)
        await websocket.close(code=1008)
        return
    await websocket.accept()
    _WS_LOOP = asyncio.get_running_loop()
    WS_CLIENTS.add(websocket)
    if _ws_is_remote(websocket):
        WS_REMOTE.add(websocket)
        print(f"[WS] remote client connected (host={websocket.headers.get('host')!r}, "
              f"via={websocket.headers.get('x-forwarded-for')!r})", flush=True)
    else:
        print("[WS] client connected", flush=True)
    # Whose conversation this socket's turns join: the phone's or this PC's
    # browser, unless "hello" names the desktop window or a session of its own.
    client_key = "remote" if _ws_is_remote(websocket) else "local"
    _SOCKETS[client_key] = websocket
    await _flush_undelivered(websocket, client_key)     # a reply this client missed

    async def _send_response(payload: dict):
        """Send a turn's reply to the client's CURRENT socket, or keep it for
        the next one. The desktop window dropped its socket during 25 s
        turns and the reply died with it (2026-09-15)."""
        await _deliver(client_key, payload)
    # Per-connection feedback policy: each client decides (and hears) its own
    # cues; the synthesizer and audio cache are shared module-wide.
    vf = _vf.Policy()
    vf.set_synthesizer(tts_to_b64)

    # Capture THIS connection's loop once: job events fire from worker threads
    # where asyncio.get_event_loop() raises RuntimeError, which silently killed
    # every job->HUD broadcast and proactive announcement.
    _ws_loop = asyncio.get_running_loop()

    # Progress queue: tools push mid-task status here from the think() thread,
    # and a background coroutine pops them and speaks them over the WS.
    import queue as _queue
    _progress_q = _queue.Queue()

    # Job registry -> HUD bridge + Phase 8 proactive voice (speak completions unprompted).
    from cygnus.jobs import registry as _jobs
    def _on_job_event(event):
        try:
            loop = _ws_loop
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    _broadcast_job(event), loop)
                # Phase 8: proactive voice for terminal states
                try:
                    from cygnus.memory import hygiene as _mh
                    line = _mh.proactive_job_announcement(event)
                    if line:
                        # Route through the policy engine (high priority, but
                        # still deduped) so terminal announcements never double
                        # up with progress-channel speech.
                        spoken = vf.announce(f"job_{event.get('state')}", line)
                        if spoken:
                            asyncio.run_coroutine_threadsafe(_speak_proactive(spoken), loop)
                    # Phase B: autonomous step milestones ("Step one done.").
                    if event.get("state") == "running":
                        step = _vf.step_line(event.get("note") or "")
                        if step:
                            spoken = vf.announce("milestone", step)
                            print(f"[FEEDBACK] step cue -> {spoken!r}", flush=True)
                            if spoken:
                                asyncio.run_coroutine_threadsafe(
                                    _speak_proactive(spoken), loop)
                    # Cancel this job's milestone timer once it finishes.
                    if event.get("state") in ("done", "error", "timeout", "unverified"):
                        t = _TASK_MILESTONES.pop(event.get("id"), None)
                        if t:
                            t.cancel()
                except Exception:
                    pass
        except RuntimeError:
            pass

    async def _broadcast_job(event):
        try:
            # Plain title / label / tone / detail for the Active Tasks card;
            # the raw fields stay for anything that reads them.
            from cygnus.brain import plain_reply
            await websocket.send_text(json.dumps({**event, **plain_reply.card(event)}))
        except Exception:
            pass

    # Which model is answering, said when it changes for this client: kept by
    # client in router_health (notice_for), so a phone that reconnects does not
    # hear "Big Pickle ... is answering" again (the owner's ask, 2026-09-12).
    try:
        from cygnus.brain import router_health as _rh
    except Exception:
        _rh = None

    async def _say_model_notice():
        """Show and speak who is answering, when it changed since the last reply."""
        if _rh is None:
            return
        try:
            st = dict(getattr(brain, "last_stats", {}) or {})
            backend = st.get("backend") or brain.last_backend
            _note_answering_model(st, backend)        # the top bar keeps it between turns
            line = _rh.notice_for(client_key, st.get("model"), backend)
            if line:
                tts_b64 = await tts_to_b64(line)
                await websocket.send_text(json.dumps({"type": "progress", "text": line, "audio": tts_b64}))
        except Exception as e:
            print(f"[WS] model notice failed: {e}", flush=True)

    async def _speak_proactive(text: str):
        from starlette.websockets import WebSocketState
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            print(f"[SPEAK_PROACTIVE] entering: {text!r}", flush=True)
            tts_b64 = await tts_to_b64(text)
            await websocket.send_text(json.dumps({"type": "response", "text": text, "audio": tts_b64, "proactive": True}))
            print(f"[SPEAK_PROACTIVE] sent ({len(tts_b64 or '')} b64)", flush=True)
        except Exception as e:
            print(f"[SPEAK_PROACTIVE] failed: {type(e).__name__}: {e}", flush=True)

    try:
        _jobs.add_listener(_on_job_event)
    except Exception:
        pass

    # Phase 8: start hygiene loop once per process (6h interval, daemon)
    try:
        from cygnus.memory import hygiene as _mh2
        if not getattr(_mh2, "_started", False):
            _mh2.start_hygiene_loop(interval_hours=6)
            _mh2._started = True
    except Exception:
        pass

    async def _progress_reader():
        """Consume progress updates from tools and speak them as they arrive."""
        loop = asyncio.get_running_loop()
        while True:
            # Run the blocking get() in a thread so it doesn't stall the event loop.
            msg = await loop.run_in_executor(None, _progress_q.get)
            if msg is None:
                break
            try:
                # Policy engine decides whether this milestone is worth SPEAKING
                # (throttled/deduped); the HUD caption updates either way.
                spoken = vf.announce("progress", msg)
                tts_b64 = await tts_to_b64(spoken) if spoken is not None else None
                await websocket.send_text(json.dumps({
                    "type": "progress", "text": msg, "audio": tts_b64
                }))
            except Exception:
                # WS closed while task still running — drain the queue silently
                # so the think() thread doesn't block on a full queue.
                break

    def _push_progress(msg):
        """Called from the think() thread to enqueue a progress update."""
        _progress_q.put(msg)

    async def _end_failed_turn(wd, reader):
        """What a turn that raised still owes: no 'still working' cue after the
        failure, and its progress reader ended. A reader left waiting took the
        next turn's end signal, so that turn waited on its own reader forever."""
        if wd is not None:
            wd.stop()
        if reader is not None and not reader.done():
            _progress_q.put(None)
            try:
                await asyncio.wait_for(reader, 5)
            except Exception:
                reader.cancel()

    async def _rescue_reply(client, response, delivered, err, retry=None, speak=False):
        """A turn that raised after think() answered still owes that answer:
        it goes to the client's current socket, or waits for the next one.
        The desktop window dropped its socket during 25 s turns and the
        answer died with it (2026-09-15). A turn with no answer yet gets the
        error line instead; nothing here may raise past the handler."""
        try:
            if isinstance(response, str) and response.strip() and not delivered \
                    and not response.startswith("⟳ HERMES_BACKGROUND:"):
                audio = None
                if speak:
                    try:
                        audio = await tts_to_b64(response)
                    except Exception:
                        pass
                await _deliver(client, {"type": "response", "text": strip_ai_artifacts(response), "audio": audio})
                await _deliver(client, {"type": "status", "state": "idle"})
            else:
                await _send_error(err, retry=retry, speak=speak)
        except Exception as e2:
            print(f"[WS] reply rescue failed: {type(e2).__name__}: {e2}", flush=True)

    async def _send_error(err, retry=None, speak=False):
        """A failed turn in plain words (plain_reply.for_error), spoken when the
        turn was. `retry` is what the user asked, so the HUD can send it again."""
        from cygnus.brain import plain_reply
        line = plain_reply.for_error(err)
        audio = None
        if speak:
            try:
                audio = await tts_to_b64(line)
            except Exception:
                pass
        await websocket.send_text(json.dumps({
            "type": "error", "text": line, "audio": audio, "retry": retry}))

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            mtype = message.get("type")

            if mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif mtype == "hello":
                if message.get("client") == "desktop":
                    WS_DESKTOP.add(websocket)
                    client_key = "desktop"
                    print("[WS] desktop app window connected", flush=True)
                if message.get("session"):
                    client_key = "session:" + str(message["session"])[:64]
                if client_key not in ("remote", "local"):
                    _SOCKETS[client_key] = websocket
                    await _flush_undelivered(websocket, client_key)

            elif mtype == "speech":
                # Phase C: duck music while JARVIS speaks, restore when done.
                # Failsafe auto-unduck guards against a lost "end" message.
                state = (message.get("state") or "").strip().lower()
                if state in ("start", "end"):
                    try:
                        wake_engine.speaking(state == "start")   # tells its own voice from the owner's
                    except Exception:
                        pass
                try:
                    if state == "start":
                        await asyncio.to_thread(voice_ducking.duck)
                    elif state == "end":
                        await asyncio.to_thread(voice_ducking.unduck)
                except Exception as e:
                    print(f"[DUCK] {state} failed: {e}", flush=True)

            elif mtype == "listening":
                # The HUD is recording a command: it owns the mic, so the
                # server-side wake fallback holds off (capped, in case "end"
                # is lost) instead of decoding over the user's voice.
                try:
                    if message.get("state") == "start":
                        wake_engine.hold(_WAKE_HOLD_MS)
                    else:
                        wake_engine.release()
                except Exception:
                    pass
                # Music down while the owner talks, browsers included: music at
                # full volume was the loudest thing in the clip. A phone listens
                # somewhere else, so the PC's music stays as it is.
                if not _ws_is_remote(websocket):
                    try:
                        if message.get("state") == "start":
                            await asyncio.to_thread(voice_ducking.duck, True)
                        else:
                            await asyncio.to_thread(voice_ducking.unduck)
                    except Exception as e:
                        print(f"[DUCK] listening {message.get('state')} failed: {e}", flush=True)

            elif mtype == "wake_ack":
                # What the HUD did with a server wake, next to what the wake
                # engine heard: a wake that arrived but opened nothing, and in
                # which view (panel, full HUD, orb, hidden in the tray).
                try:
                    wake_engine.note(
                        f"HUD {message.get('result')} (mode {message.get('mode')}, "
                        f"view {message.get('view')}, hidden {message.get('hidden')})")
                except Exception:
                    pass

            elif mtype == "audio":
                # Browser sent a recorded clip (base64 WAV or WebM/Opus blob).
                # mode == "scan" -> transcribe only (client-side wake-word check),
                # no Gemini call, no TTS. Anything else keeps the original behaviour.
                audio_b64 = message.get("data", "")
                scan = message.get("mode") == "scan"
                # The browser's own WakeListener fired to produce this clip, so the
                # server-side wake engine must not also open a capture for the same
                # "jarvis". Tell it the wake was already handled client-side.
                try:
                    wake_engine.note_browser_wake()
                except Exception:
                    pass
                if not audio_b64:
                    await websocket.send_text(json.dumps({
                        "type": "error", "text": "I didn't get any sound from the microphone."}))
                    continue

                if not scan:
                    await websocket.send_text(json.dumps({"type": "status", "state": "transcribing"}))
                    # The clip is in: the music the listening duck lowered comes
                    # back now, before any reply (which ducks it again for itself).
                    try:
                        await asyncio.to_thread(voice_ducking.unduck)
                    except Exception:
                        pass

                # Stage timing. Without this the only way to find out where a
                # turn goes is to stop the server and benchmark by hand.
                _t = {"start": time.time()}

                # The wake loop and this turn share 16 cores. Let the turn have
                # them: the browser's own WakeListener still covers wake words,
                # and a wake fired mid-turn is ignored by the HUD anyway.
                try:
                    wake_engine.pause()
                except Exception:
                    pass

                text, _wd, reader_task = None, None, None
                try:
                    audio_bytes = base64.b64decode(audio_b64)
                    wav_path = decode_audio_to_wav(audio_bytes)
                    _t["decode"] = time.time()

                    # Transcribe with Whisper, checked against the browser's live
                    # transcript when the HUD sent one (transcript_pick).
                    hint = "" if scan else " ".join(str(message.get("hint") or "").split())[:300]
                    source = "whisper"
                    # A wake-opened clip starts a few seconds before the wake
                    # (the HUD's pre-roll) so the command is never clipped; the
                    # command is what follows the wake word.
                    clean = after_wake if message.get("preroll") and not scan else None
                    if hint:
                        text, source, heard, conf = voice_engine.transcribe_with_hint(
                            wav_path, hint, known=_known_names(), clean=clean)
                        print(f"[STT] whisper: {heard[:120]!r} (conf {conf:.2f}) | browser: "
                              f"{hint[:120]!r} -> {source}", flush=True)
                    else:
                        text = voice_engine._transcribe_from_file(wav_path)
                        if clean:
                            print(f"[STT] pre-rolled: {text!r}", flush=True)
                            # Nothing but the wake, or scraps of it ("Hey Cygnus,
                            # up." left "up."): no word of three letters or more.
                            bare_wake = bool(text.strip()) and not any(
                                len(w) > 2 for w in _re.findall(r"[a-z']+", clean(text).lower()))
                            text = clean(text)
                            if bare_wake:
                                # Only "Cygnus" was said: the command is still
                                # to come, so the HUD listens again.
                                os.unlink(wav_path)
                                await websocket.send_text(json.dumps({"type": "relisten"}))
                                continue
                    _t["stt"] = time.time()
                    print(f"[STT] raw{' (scan)' if scan else ''}: {text!r}", flush=True)
                    os.unlink(wav_path)

                    if source == "unsure" and not scan:
                        # Neither transcript is trustworthy: ask, never answer
                        # a sentence nobody said.
                        line = "I didn't catch that, sir. Could you say it again?"
                        await websocket.send_text(json.dumps({
                            "type": "response", "text": line, "audio": await tts_to_b64(line)}))
                        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                        continue

                    if scan:
                        await websocket.send_text(json.dumps({
                            "type": "scan", "text": (text or "").strip()
                        }))
                        continue

                    if not text or not text.strip():
                        await websocket.send_text(json.dumps({
                            "type": "status", "state": "idle",
                            "text": "(no speech detected)"
                        }))
                        continue

                    # ---- Layer 1: Intake normalization + confirm gate ----
                    # Fixes bad voice/grammar before the router sees it.
                    # Clarify ONLY when we have an uncertain ENTITY guess
                    # ("play talor swift" -> did you mean taylor swift?).
                    # A verb with NO entity ('open' clipped by music) routes
                    # to the brain normally — it resolves apps far better
                    # than the tiny corpus, and asking 'did you mean open,
                    # the beatles?' for a clipped command is worse than trying.
                    intent = resolve_intent(text.strip(), with_names(INTAKE_CORPUS, _known_names()))
                    from cygnus.brain import action_gate    # in lead the gate asks this, for every input path
                    if (intent.needs_confirmation() and intent.candidates
                            and intent.entity is not None and action_gate.MODE != "lead"):
                        await websocket.send_text(json.dumps({
                            "type": "clarify",
                            "heard": text.strip(),
                            "echo": intent.echo,
                            "candidates": intent.candidates,
                        }))
                        await websocket.send_text(json.dumps({
                            "type": "status", "state": "idle",
                            "text": intent.echo
                        }))
                        continue

                    await websocket.send_text(json.dumps({
                        "type": "transcript", "text": text.strip()
                    }))
                    await websocket.send_text(json.dumps({"type": "status", "state": "thinking"}))
                    _wd = _ThinkingWatchdog(websocket, vf)
                    _wd.start()
                    response, delivered = None, False

                    loop = asyncio.get_running_loop()

                    # Start the progress reader for mid-task voice updates.
                    reader_task = asyncio.create_task(_progress_reader())

                    def on_hermes_done(result):
                        # Called from the background Hermes thread; hop back to the
                        # event loop to ship the answer to the client. The
                        # answer also joins that client's history.
                        brain.note_late_result(result, client=client_key)
                        asyncio.run_coroutine_threadsafe(
                            deliver_result(websocket, result, speak=True, client=client_key), loop)

                    # Off the event loop: a turn that drives the browser can run for
                    # a minute, and blocking here stalls the WebSocket for its duration.
                    response = await asyncio.to_thread(
                        brain.think, text.strip(), on_hermes_done, _push_progress, client=client_key,
                        remote=_ws_is_remote(websocket))
                    _view = (getattr(brain, "last_stats", None) or {}).get("view")
                    _t["brain"] = time.time()
                    _wd.stop()
                    _arm_phase_b(websocket, vf, response)

                    # The transcript already went out before think() started (that is
                    # the point — the user sees their words while JARVIS works).
                    # Re-sending it here made every voice turn emit two identical
                    # transcript messages, which the HUD would render as a duplicate
                    # user line now that it logs them.
                    sstore_log("user", text.strip())

                    # Signal the progress reader that think() is done.
                    _progress_q.put(None)
                    await reader_task

                    # Phase 4: if Hermes was delegated in the background, think()
                    # returns the "working" ack immediately. Send it, free the mic
                    # (state idle), and let deliver_result push the real answer later.
                    if isinstance(response, str) and response.startswith("⟳ HERMES_BACKGROUND:"):
                        ack = response.split(":", 1)[1].strip()
                        ack = strip_ai_artifacts(ack)
                        sstore_log("assistant", ack)
                        tts_b64 = await tts_to_b64(ack)
                        await websocket.send_text(json.dumps({
                            "type": "response", "text": ack, "audio": tts_b64, "deferred": False
                        }))
                        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                        continue

                    sstore_log("assistant", response)
                    # Nothing before the reply may raise on a dead socket: the
                    # reply goes to the client's current socket (_deliver).
                    try:
                        await _say_model_notice()
                    except Exception:
                        pass
                    await _deliver(client_key, {"type": "status", "state": "speaking"})
                    tts_b64 = await tts_to_b64(response)
                    _t["tts"] = time.time()
                    _log_turn_timing(_t)
                    try:
                        wake_engine.suspend()
                    except Exception:
                        pass
                    await _send_response({
                        "type": "response",
                        "text": strip_ai_artifacts(response),
                        "audio": tts_b64
                    })
                    delivered = True
                    await _send_view(_view)
                    try:
                        await send_telemetry(_SOCKETS.get(client_key) or websocket)
                    except Exception:
                        pass
                    await _deliver(client_key, {"type": "status", "state": "idle"})

                except Exception as e:
                    print(f"[WS] audio error: {type(e).__name__}: {e}", flush=True)
                    await _end_failed_turn(_wd, reader_task)
                    await _rescue_reply(client_key, response, delivered, e,
                                        retry=None if scan else (text or "").strip() or None, speak=not scan)
                finally:
                    try:
                        wake_engine.resume()
                    except Exception:
                        pass

            elif mtype == "command":
                # Client-side slash commands. Kept off the "text" path on
                # purpose: routing "/clear" through the brain would spend a whole
                # local turn deciding what to do with it, and the model might
                # answer instead of clearing anything.
                name = (message.get("name") or "").strip().lower()
                if name == "clear":
                    brain.reset(client=client_key)
                    TURN_STATS["turns"] = 0
                    TURN_STATS["by_backend"].clear()
                    TURN_STATS["latencies"].clear()
                    print("[WS] context cleared", flush=True)
                    await websocket.send_text(json.dumps({
                        "type": "cleared",
                        "text": "Context cleared. Starting fresh."
                    }))
                    await send_telemetry(websocket)
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                elif name == "redirect":
                    # Phase 4: mid-flight redirect — inject a new instruction into
                    # the warm Hermes session without starting a fresh task.
                    redirect_text = (message.get("text") or "").strip()
                    if not redirect_text:
                        await websocket.send_text(json.dumps({
                            "type": "error", "text": "redirect: no instruction given"
                        }))
                        continue
                    try:
                        from cygnus.jobs.warm_harness import warm_redirect
                        await websocket.send_text(json.dumps({
                            "type": "status", "state": "thinking"
                        }))
                        loop = asyncio.get_running_loop()
                        result = await asyncio.to_thread(warm_redirect, redirect_text)
                        if result is None:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "text": "No active Hermes session to redirect."
                            }))
                        else:
                            sstore_log("assistant", result)
                            await websocket.send_text(json.dumps({
                                "type": "response", "text": result,
                                "audio": await tts_to_b64(result)
                            }))
                    except Exception as e:
                        print(f"[WS] redirect error: {e}", flush=True)
                        await _send_error(e)
                    await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error", "text": f"unknown command: {name}"
                    }))

            elif mtype == "app_command":
                # "/hermes start a new session then prompt: …": one step runs
                # at once; more than one comes back as a plan for Run / Cancel.
                # Detached from this socket (see _app_command); the reply
                # reaches whatever socket this client has when it is ready.
                slug = (message.get("target") or "").strip().lower()
                said = (message.get("text") or "").strip()
                sstore_log("user", f"/{slug} {said}".strip())
                task = asyncio.create_task(_app_command(client_key, slug, said, bool(message.get("speak", True))))
                _APP_TASKS.add(task)
                task.add_done_callback(_APP_TASKS.discard)

            elif mtype in ("app_run", "app_cancel"):
                held = _APP_PLANS.pop(str(message.get("id") or ""), None)
                if mtype == "app_cancel":
                    continue
                if not held or time.time() - held[2] > _APP_PLAN_S:
                    await websocket.send_text(json.dumps({"type": "error",
                                                          "text": "That plan expired, sir. Send the command again."}))
                    continue
                task = asyncio.create_task(_run_app_command(client_key, held[0], held[1],
                                                            bool(message.get("speak", True))))
                _APP_TASKS.add(task)
                task.add_done_callback(_APP_TASKS.discard)

            elif mtype == "cue":
                # Spoken session cue (wake / close). The client requests a
                # short line ("what is it, sir?" / "standing by, sir") and the
                # server TTS-es it so JARVIS audibly signals listening state.
                cue_text = (message.get("text") or "").strip()
                if not cue_text:
                    continue
                try:
                    tts_b64 = await tts_to_b64(cue_text)
                    await websocket.send_text(json.dumps({
                        "type": "cue", "text": cue_text, "audio": tts_b64
                    }))
                except Exception as e:
                    print(f"[WS] cue TTS failed: {e}", flush=True)

            elif mtype == "text":
                # Direct text command. TTS audio only when "speak": true
                # (wake-word client uses this when the command arrived in the
                # same breath as "Jarvis", so it never re-records).
                user_text = message.get("text", "").strip()
                if not user_text:
                    continue
                speak = bool(message.get("speak"))
                await websocket.send_text(json.dumps({"type": "transcript", "text": user_text}))
                await websocket.send_text(json.dumps({"type": "status", "state": "thinking"}))
                _wd = _ThinkingWatchdog(websocket, vf)
                _wd.start()
                reader_task = None
                response, delivered = None, False
                try:
                    loop = asyncio.get_running_loop()

                    # Start the progress reader: tools will push mid-task status
                    # and this coroutine speaks them as they arrive.
                    reader_task = asyncio.create_task(_progress_reader())

                    def on_hermes_done(result):
                        brain.note_late_result(result, client=client_key)   # joins the history
                        asyncio.run_coroutine_threadsafe(
                            deliver_result(websocket, result, speak, client=client_key), loop)

                    response = await asyncio.to_thread(
                        brain.think, user_text, on_hermes_done, _push_progress, client=client_key,
                        remote=_ws_is_remote(websocket))
                    _view = (getattr(brain, "last_stats", None) or {}).get("view")
                    _wd.stop()
                    try:
                        _arm_phase_b(websocket, vf, response)
                    except Exception:
                        pass
                    sstore_log("user", user_text)

                    # Signal the progress reader that think() is done, then
                    # drain any remaining messages it queued.
                    _progress_q.put(None)
                    await reader_task

                    # Phase 4: background Hermes delegation — ack now, answer later.
                    if isinstance(response, str) and response.startswith("⟳ HERMES_BACKGROUND:"):
                        ack = response.split(":", 1)[1].strip()
                        ack = strip_ai_artifacts(ack)
                        sstore_log("assistant", ack)
                        tts_b64 = await tts_to_b64(ack) if speak else None
                        await websocket.send_text(json.dumps({
                            "type": "response", "text": ack, "audio": tts_b64, "deferred": False
                        }))
                        await websocket.send_text(json.dumps({"type": "status", "state": "idle"}))
                        continue

                    sstore_log("assistant", response)
                    try:
                        await _say_model_notice()
                    except Exception:
                        pass
                    tts_b64 = None
                    if speak:
                        await _deliver(client_key, {"type": "status", "state": "speaking"})
                        tts_b64 = await tts_to_b64(response)
                        try:
                            wake_engine.suspend()
                        except Exception:
                            pass
                    await _send_response({
                        "type": "response", "text": strip_ai_artifacts(response), "audio": tts_b64
                    })
                    delivered = True
                    await _send_view(_view)
                    try:
                        await send_telemetry(_SOCKETS.get(client_key) or websocket)
                    except Exception:
                        pass
                    await _deliver(client_key, {"type": "status", "state": "idle"})
                except Exception as e:
                    print(f"[WS] text error: {type(e).__name__}: {e}", flush=True)
                    await _end_failed_turn(_wd, reader_task)
                    await _rescue_reply(client_key, response, delivered, e, retry=user_text, speak=speak)

            else:
                await websocket.send_text(json.dumps({
                    "type": "error", "text": f"unknown message type: {mtype}"
                }))

    except WebSocketDisconnect:
        print("[WS] client disconnected", flush=True)
        WS_CLIENTS.discard(websocket)
    except Exception as e:
        print(f"[WS] error: {e}", flush=True)
        WS_CLIENTS.discard(websocket)
    finally:
        WS_REMOTE.discard(websocket)
        WS_DESKTOP.discard(websocket)
        if _SOCKETS.get(client_key) is websocket:   # a newer socket for this client stays
            _SOCKETS.pop(client_key, None)
        # This connection's job listener must not outlive it: stale listeners
        # spoke every finished job into closed sockets (5x after 5 reconnects).
        try:
            _jobs.remove_listener(_on_job_event)
        except Exception:
            pass


# The model that last answered a turn. A Hermes or tool turn records no model,
# so the top bar fell back to the configured one ("Gemini 3.8 Flash") while
# Big Pickle was the one answering (2026-09-12).
_LAST_ANSWERED = {"model": None}


def _note_answering_model(stats: dict, backend: str | None) -> None:
    from cygnus.config import OLLAMA_MODEL
    if (stats or {}).get("model"):
        _LAST_ANSWERED["model"] = stats["model"]
    elif backend == "ollama":
        _LAST_ANSWERED["model"] = OLLAMA_MODEL


def _warm_running():
    """The first scan reads every process's command line (seconds); later
    ones reuse them (~0.1 s). Take the slow one at startup, off any request."""
    try:
        from cygnus.apps import running as running_apps
        running_apps.inventory()
    except Exception as e:
        print(f"[running] warm-up failed: {e!r}", flush=True)


threading.Thread(target=_warm_running, daemon=True, name="running-warm").start()


@app.get("/running")
async def get_running():
    """What is running on this machine and what it is doing (running_apps).
    Read-only; the HUD polls it while its Running Now section is visible."""
    from cygnus.apps import running as running_apps
    rows = await asyncio.to_thread(running_apps.inventory)
    return _no_cache(JSONResponse({"rows": rows, "ts": time.time()}))


# Acting on a row (phases 4-5). The phone may show, read, capture and
# describe a window (owner, 2026-09-14); only stopping is the laptop's.
def _http_is_remote(request: Request) -> bool:
    """_ws_is_remote for a plain request: proxied, or not addressed as loopback."""
    if request.headers.get("x-forwarded-for"):
        return True
    return _hostname(request.headers.get("host", "")) not in ("127.0.0.1", "localhost", "[::1]")


def _laptop_only():
    return JSONResponse({"text": "Stopping a program is done from the laptop."}, status_code=403)


def _running_row(row_id):
    from cygnus.apps import running as running_apps
    return next((r for r in running_apps.inventory() if r["id"] == str(row_id)), None)


_STOP_TOKENS: dict = {}          # token -> (plan, time): a stop waiting for the HUD's confirm
_STOP_TOKEN_S = 120


@app.get("/running/text")
async def running_text(request: Request, id: str):
    from cygnus.apps import running as running_apps
    row = await asyncio.to_thread(_running_row, id)
    text = await asyncio.to_thread(running_apps.window_text, row) if row else ""
    return _no_cache(JSONResponse({"text": text}))


@app.get("/running/thumb")
async def running_thumb(request: Request, id: str, full: int = 0):
    """A picture of a row's window (JPEG) for a HUD without Electron's
    thumbnails: the phone, a browser tab. full=1 is the large one."""
    from fastapi.responses import Response
    from cygnus.apps import running as running_apps
    row = await asyncio.to_thread(_running_row, id)
    wins = (row or {}).get("windows") or []
    img = await asyncio.to_thread(running_apps.capture_window, wins[0]["hwnd"],
                                  1600 if full else 480, "JPEG") if wins else None
    if not img:
        return JSONResponse({"text": "No picture of that window."}, status_code=404)
    return Response(img, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=20"})


@app.post("/running/front")
async def running_front(request: Request, id: str):
    # Allowed from the phone too: it only raises a window (owner, 2026-09-14).
    from cygnus.apps import running as running_apps
    row = await asyncio.to_thread(_running_row, id)
    ok, text = await asyncio.to_thread(running_apps.bring_to_front, row)
    return JSONResponse({"ok": ok, "text": text})


@app.get("/running/describe")
async def running_describe(request: Request, id: str):
    from cygnus.apps import running as running_apps
    row = await asyncio.to_thread(_running_row, id)
    answer, model = await asyncio.to_thread(running_apps.describe_window, row)
    return _no_cache(JSONResponse({"text": answer or f"I couldn't see it: {model}.",
                                   "model": model if answer else None}))


# "/hermes <instruction>" (app_commands, APP_COMMANDS_PLAN.md): the "/" menu,
# and a plan of more than one step held for the owner's Run for five minutes.
_APP_PLANS: dict = {}
_APP_PLAN_S = 300


@app.get("/app/targets")
async def app_targets():
    from cygnus.apps import commands as app_commands
    return _no_cache(JSONResponse({"targets": await asyncio.to_thread(app_commands.targets)}))


_APP_WORK_S = 330          # understanding plus a Hermes turn with tools


async def _say(client_key: str, text: str, speak: bool, kind: str = "response") -> None:
    """A reply to a client, into its conversation and its current socket."""
    if kind == "response":
        brain.note_late_result(text, client=client_key)
    sstore_log("assistant", text)
    audio = await tts_to_b64(text) if speak and kind == "response" else None
    await _deliver(client_key, {"type": kind, "text": text, "audio": audio})
    await _deliver(client_key, {"type": "status", "state": "idle"})


async def _run_app_command(client_key: str, slug: str, steps: list, speak: bool):
    """Carry out an app command off the event loop; the reply joins the
    client's conversation like any other answer."""
    from cygnus.apps import commands as app_commands
    print(f"[apps] /{slug} run: {[app_commands.describe(s) for s in steps]}", flush=True)
    await _deliver(client_key, {"type": "status", "state": "thinking"})
    try:
        result = await asyncio.wait_for(asyncio.to_thread(app_commands.run, slug, steps), _APP_WORK_S)
    except asyncio.TimeoutError:
        await _say(client_key, f"/{slug} is taking too long, sir; I stopped waiting for it.", speak, "error")
        return
    except Exception as e:
        print(f"[apps] /{slug} failed: {type(e).__name__}: {e}", flush=True)
        await _say(client_key, f"/{slug} didn't work: {e}", speak, "error")
        return
    print(f"[apps] /{slug} -> {result[:160]!r}", flush=True)
    try:
        from cygnus.brain import agent_loop
        agent_loop.remember_slash(client_key, slug, " / ".join(app_commands.describe(s) for s in steps), result)
    except Exception:
        pass
    await _say(client_key, result, speak)


async def _app_command(client_key: str, slug: str, said: str, speak: bool):
    """"/hermes …" as a task of its own, detached from the socket it came on:
    the phone's socket drops when its screen dims, and a command tied to it
    was cancelled with it (2026-09-15). The model reads the instruction
    against the app's own actions; what it has no action for is said, never
    guessed. One step runs; more come back as a plan for Run / Cancel."""
    from cygnus.apps import commands as app_commands
    import secrets
    try:
        from cygnus.brain import agent_loop
        agent_loop._current_app[client_key] = slug   # follow-ups without the slash stay with it
    except Exception:
        pass
    print(f"[apps] /{slug} {said!r} from {client_key}", flush=True)
    await _deliver(client_key, {"type": "status", "state": "thinking"})
    try:
        read = await asyncio.wait_for(asyncio.to_thread(app_commands.understand, slug, said), 120)
    except Exception as e:
        print(f"[apps] /{slug} understand failed: {type(e).__name__}: {e}", flush=True)
        await _say(client_key, f"I couldn't work out what to do in /{slug}, sir: {type(e).__name__}.", speak, "error")
        return
    steps = read["steps"]
    if not read["target"]:
        await _say(client_key, f"Nothing called /{slug} is running, sir.", speak)
    elif not read["target"]["driver"]:
        await _say(client_key, f"I can't drive {read['target']['name']} yet, sir: it has no actions or rules I can use.", speak)
    elif read["missing"] or not steps:
        await _say(client_key, app_commands.cannot_reply(read["target"], read["missing"] or said,
                                                         read["actions"], bool(read.get("no_model"))), speak)
    elif len(steps) == 1 and not steps[0].get("ask"):
        await _run_app_command(client_key, slug, steps, speak)
    else:
        for old in [k for k, (_, _, at) in _APP_PLANS.items() if time.time() - at > _APP_PLAN_S]:
            del _APP_PLANS[old]
        plan_id = secrets.token_hex(4)
        _APP_PLANS[plan_id] = (slug, steps, time.time())
        print(f"[apps] /{slug} plan {plan_id}: {[app_commands.describe(s) for s in steps]}", flush=True)
        await _deliver(client_key, {"type": "app_plan", "id": plan_id, "target": slug,
                                    "steps": [app_commands.describe(s) for s in steps]})
        await _deliver(client_key, {"type": "status", "state": "idle"})


_APP_TASKS: set = set()               # detached tasks, kept until done


@app.post("/running/stop")
async def running_stop(request: Request):
    """Two steps: {id} returns what would stop and a token; {token} within two
    minutes stops exactly that (running_apps.stop_now checks the processes)."""
    if _http_is_remote(request):
        return _laptop_only()
    import secrets
    from cygnus.apps import running as running_apps
    body = await request.json()
    for t in [t for t, (_, at) in _STOP_TOKENS.items() if time.time() - at > _STOP_TOKEN_S]:
        del _STOP_TOKENS[t]
    if body.get("token"):
        held = _STOP_TOKENS.pop(str(body["token"]), None)
        if not held:
            return JSONResponse({"text": "That stop request expired. Ask again."})
        return JSONResponse({"done": True, "text": await asyncio.to_thread(running_apps.stop_now, held[0])})
    row = await asyncio.to_thread(_running_row, body.get("id"))
    plan, why = await asyncio.to_thread(running_apps.stop_plan, row)
    if not plan:
        return JSONResponse({"text": f"I won't stop that: {why}."})
    token = secrets.token_hex(8)
    _STOP_TOKENS[token] = (plan, time.time())
    n = len(plan["pids"])
    return JSONResponse({"token": token,
                         "confirm": f"Stop {plan['what']}? That ends {n} process{'es' if n != 1 else ''}."})


@app.get("/status")
async def get_status():
    """Report the backend actually in use, not a hardcoded guess."""
    from cygnus.config import OLLAMA_MODEL, OLLAMA_BASE_URL
    # Ollama was missing from this chain entirely, so with local-only on (every
    # cloud flag False) it fell through to the Gemini branch and reported
    # brain=gemini-2.5-flash / endpoint=google-genai while actually running the
    # local model. That readout is why JARVIS looked like it kept switching.
    # Gemini is intentionally disabled (its key is reserved for the user's
    # portfolio), so it is never the resolved model/endpoint.
    if getattr(brain, "_local_only", False) or getattr(brain, "_prefer_local", False):
        model, endpoint = OLLAMA_MODEL, OLLAMA_BASE_URL
    elif brain._use_router:
        model, endpoint = ROUTER_MODEL, ROUTER_BASE_URL
    elif brain._use_cerebras:
        model, endpoint = CEREBRAS_MODEL, CEREBRAS_BASE_URL
    elif brain._use_groq:
        model, endpoint = GROQ_MODEL, GROQ_BASE_URL
    elif getattr(brain, "_use_ollama", False):
        model, endpoint = OLLAMA_MODEL, OLLAMA_BASE_URL
    else:
        model, endpoint = OLLAMA_MODEL, OLLAMA_BASE_URL
    # "brain" used to report the configured router model even when a different
    # backend answered, so the HUD showed brain=ag/claude-sonnet-4-6 next to
    # answered-by=ollama and looked self-contradictory. Report what actually ran.
    _st = getattr(brain, "last_stats", {}) or {}
    _note_answering_model(_st, _st.get("backend") or getattr(brain, "last_backend", None))
    actual = _st.get("model") or _LAST_ANSWERED["model"]
    # Route reflects the actual chain: router -> cerebras -> groq -> local.
    # Gemini is intentionally omitted (reserved for portfolio work).
    if getattr(brain, "_local_only", False):
        route = "local only"
    else:
        hops = []
        if getattr(brain, "_prefer_local", False):
            hops.append("local")
        if brain._use_router:
            hops.append("router")
        from cygnus.brain import google_ai
        if google_ai.enabled():
            hops.append("google")               # the agent loop's own Gemini key
        if brain._use_cerebras:
            hops.append("cerebras")
        if brain._use_groq:
            hops.append("groq")
        if getattr(brain, "_use_ollama", False) and not getattr(brain, "_prefer_local", False):
            hops.append("local")
        route = " > ".join(hops)
    return {
        "status": "running",
        "brain": actual or model,
        # The same model as the user would name it, for the top bar.
        "brain_label": __import__("cygnus.brain.router_health", fromlist=["_"]).friendly(actual or model),
        "configured": model,
        "local_only": bool(getattr(brain, "_local_only", False)),
        "route": route,
        "endpoint": endpoint,
        # Gemini is off; the final real fallback is the local model.
        "fallback": None if getattr(brain, "_local_only", False) else OLLAMA_MODEL,
        # Whether 9router answers, how often Cygnus had to start it, and the
        # models it would walk right now (router_keeper, router_health).
        "router_up": __import__("cygnus.brain.router_keeper", fromlist=["_"]).port_open(),
        "router_restarts": __import__("cygnus.brain.router_keeper", fromlist=["_"]).state.get("started", 0),
        "router_restarts_hung": __import__("cygnus.brain.router_keeper", fromlist=["_"]).state.get("restarts_hung", 0),
        "router_models": __import__("cygnus.brain.router_health", fromlist=["_"]).healthy_models()[:8],
        "last_backend": brain.last_backend,
        "voice": f"whisper:{WHISPER_MODEL}",
        "tts": _TTS_ENGINE,
        # The HUD's "Vault index" row read an em-dash forever outside demo mode
        # (2026-09-23): nothing on /status answered it. Cheap to answer - a
        # stat, no directory walk.
        "vault": __import__("cygnus.tools.vault", fromlist=["_"]).index_state(),
        "feedback_mode": _vf.current_mode(),
        # Background job table (all tiers) for HUD/programmatic polling.
        "jobs_active": __import__("cygnus.jobs.registry", fromlist=["_"]).active(),
        "jobs_recent": __import__("cygnus.jobs.registry", fromlist=["_"]).recent(8),
        "jobs_status_line": __import__("cygnus.jobs.registry", fromlist=["_"]).status_line(),
        # The same cards the job events carry, so a page that just opened
        # shows what is running and what ended in the last 15 minutes.
        "job_cards": _job_cards(),
        # Server-side wake-word fallback health (music-proof). 'healthy': mic live
        # and listening; false just means the fallback is off — the browser's own
        # Web Speech WakeListener still works on its own.
        "wake_server": {
            "healthy": bool(getattr(wake_engine, "healthy", False)),
            "error": getattr(wake_engine, "last_err", "") or None,
        },
    }


@app.post("/voice_feedback")
async def post_voice_feedback(message: Request):
    """HUD toggle: switch the voice-feedback mode at runtime."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    try:
        mode = _vf.set_mode(body.get("mode") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    print(f"[FEEDBACK] mode -> {mode}", flush=True)
    return {"ok": True, "mode": mode}


@app.get("/music/now")
async def get_music_now():
    """Now-playing metadata for the HUD card. Idle when no music window is open."""
    from cygnus.agents import music as music_agent
    try:
        info = await asyncio.to_thread(music_agent.now_playing)
    except Exception as e:
        print(f"[MUSIC] now_playing failed: {e}", flush=True)
        info = {"idle": True}
    return info


@app.post("/music")
async def post_music(message: Request):
    """Music controls from the HUD card. stop_yt pauses/navigates YT Music away;
    spotify_toggle presses the media play/pause key (toggles Spotify)."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    action = (body.get("action") or "").strip().lower()
    if action == "stop_yt":
        from cygnus.agents import music as music_agent
        out = await asyncio.to_thread(music_agent.stop_music)
        return {"ok": True, "action": action, "result": out[:200]}
    if action == "spotify_toggle":
        from cygnus.tools import stop_spotify
        out = await asyncio.to_thread(stop_spotify)
        return {"ok": True, "action": action, "result": out[:200]}
    return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)


def main():
    """Start the server (python -m cygnus)."""
    import uvicorn
    from cygnus.jobs import registry as _jobs_owner
    _jobs_owner.claim_ownership()
    port = int(os.environ.get("JARVIS_PORT", "8000"))
    # This computer only: the tools open apps and drive the browser with no
    # login. Remote devices come in through a proxy on this machine
    # (tailscale serve); JARVIS_HOST=0.0.0.0 re-exposes it on the LAN, and the
    # LAN address must then be listed in JARVIS_ALLOWED_ORIGINS as well.
    host = os.environ.get("JARVIS_HOST", "127.0.0.1")
    print(f"[JARVIS Web] Starting on http://{host}:{port}  (ws://{host}:{port}/ws)")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    # Run as a script this module would be imported a second time by
    # speech.py and apps_routes.py (as cygnus.server.app), starting a second
    # server: the one entry point is the package.
    raise SystemExit("Start Cygnus with: python -m cygnus")
