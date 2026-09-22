# CYGNUS

A voice assistant for a Windows laptop. Say "Cygnus", ask for something, and
it answers, opens and shows apps, looks at what is running, or hands bigger
work to a coding agent and reports back. It runs as a desktop app with a
tray panel, a full HUD and a mini orb, and the same HUD works on a phone.

The project began as JARVIS. The code still uses that name in places
(`jarvis_web.py`, `jarvis_hud_v3.html`, `JARVIS_*` settings); the assistant
and the repository are Cygnus.

## What it does

- **Voice**: the wake word "Cygnus" is heard by a small Whisper model
  (`wake_engine.py`); speech is transcribed locally with faster-whisper;
  replies are spoken with Kokoro or edge-tts.
- **Brain**: turns go to a cloud model through a local router (9router, an
  OpenAI-compatible proxy), with fallback models tried in order and a local
  Ollama model as the last resort. `router_health.py` keeps track of which
  models answer and says when a stand-in is answering.
- **Apps and sites**: opens installed apps and registered websites. "Show X on
  screen" or "open X" brings an app that is already open to the front instead
  of starting a second copy.
- **Running panel**: what is running on the machine (agents, servers, apps,
  background programs) and Cygnus's own tasks in one list, with what each is
  doing. A row can bring its window forward, read its text, show a picture of
  it, or have it described; stopping a server or background program asks
  first and is laptop-only.
- **Delegation**: bigger jobs go to Claude Code, Codex, Gemini CLI or Hermes
  as background tasks. Anything that may change files asks for a confirm
  first. The result comes back as a reply and stays in the conversation, so
  "what did it change?" is answered from the record.
- **Confirm and clarify**: one shared reader for yes/no/which-one answers, and
  an act/confirm/clarify gate before actions (`consent.py`, `action_gate.py`).
- **Phone**: the HUD is served over Tailscale, so the phone can talk to
  Cygnus, see what is running, and bring a window forward.

## Running it

Requirements: Windows, Python 3.10+, Node.js (for the desktop app), a
microphone, and at least one model backend (9router or Ollama).

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # then set the router and model settings
```

The backend and HUD (port from `JARVIS_PORT` in `.env`):

```bash
.venv\Scripts\python jarvis_web.py     # then open http://127.0.0.1:<port>
```

The desktop app in `desktop/` puts the HUD in a tray panel (Ctrl+Alt+J), a
full view and a mini orb:

```bash
cd desktop
npm install
npm run unpack-electron    # only if node_modules/electron/dist has no electron.exe
npm run dev
```

If a backend is already running, the app attaches to it; otherwise it starts
one from `.venv`, restarts it if it crashes, and stops it on quit. Do not run
`watch_and_restart.py` while the app owns the backend: both would start a
server on the same port.

## Routine check

One command runs every check and writes `logs/prod-check-<time>.json`:
compile, the pytest suite, the routing gate, the running server (9router up
and answering, at least three healthy cloud models, not on the local model),
and the smoke test against it.

```bash
.venv\Scripts\python scripts\prod_check.py              # safe: nothing on the machine changes
.venv\Scripts\python scripts\prod_check.py --tier live  # also opens/closes apps, tabs, music
.venv\Scripts\python scripts\prod_check.py --no-server  # code checks only
```

Run it before a push and after each restructure step. Exit code 0 means
everything that ran passed.

A Windows scheduled task, "Cygnus prod check", runs the safe tier every day
at 09:00 (or at the next wake if the laptop was asleep). It runs hidden; the
printout goes to `logs/prod-check-last.txt`, and a failed check keeps its
last 80 lines of output in the JSON report. Remove it in Task Scheduler.

## Settings

All settings live in `.env` (see `.env.example`). The ones most often changed:

| Setting | What it does |
|---|---|
| `ROUTER_BASE_URL`, `ROUTER_MODEL`, `ROUTER_FALLBACK_MODELS` | The router and the models tried in order |
| `JARVIS_USE_OLLAMA`, `OLLAMA_MODEL` | The local fallback model |
| `WHISPER_MODEL` | Speech-to-text model size |
| `JARVIS_CLI_JOB_TIMEOUT` | How long a delegated coding job may run (seconds) |
| `JARVIS_IDLE_RESET_MINUTES` | Clear the conversation after this long idle (not while a task runs) |
| `JARVIS_PORT` | The HUD and backend port |

## Layout

| Path | What it is |
|---|---|
| `jarvis_web.py` | FastAPI backend: WebSocket turns, HUD, Running endpoints |
| `brain_gemini.py` | Routing, model chain, conversation state |
| `tools.py` | Tools: apps, sites, music, delegation, files |
| `running_apps.py` | What is running, window text, pictures, raise and stop |
| `wake_engine.py`, `voice_engine.py` | Wake word and speech |
| `jarvis_hud_v3.html` | The HUD |
| `desktop/` | Electron desktop app |
| `tests/` | pytest suite (`.venv\Scripts\python -m pytest tests`) |
| `docs/plans/`, `docs/reviews/` | Design notes and decisions for each feature; reviews |
| `scripts/prod_check.py` | The routine check (see above) |

## Author

Roger Abay Jr
