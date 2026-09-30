# CYGNUS

A voice assistant for a Windows laptop. Say "Cygnus", ask for something, and
it answers, opens and shows apps, looks at what is running, or hands bigger
work to a coding agent and reports back. It runs as a desktop app with a
tray panel, a full HUD and a mini orb, and the same HUD works on a phone.

The project began as JARVIS. Some names still say so (`jarvis_hud_v3.html`,
`JARVIS_*` settings); the assistant, the package and the repository are
Cygnus.

## What it does

- **Voice**: the wake word "Cygnus" is heard by a small Whisper model
  (`cygnus/voice/wake.py`); speech is transcribed locally with faster-whisper;
  replies are spoken with Kokoro or edge-tts.
- **Brain**: turns go to a cloud model through a local router (9router, an
  OpenAI-compatible proxy), with fallback models tried in order and a local
  Ollama model as the last resort. `cygnus/brain/router_health.py` keeps
  track of which models answer and says when a stand-in is answering, and
  `router_keeper.py` restarts 9router when it is down or hung.
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
- **Confirm and clarify**: one safety gate where actions run
  (`cygnus/safety.py`): risky actions wait for a yes in a later turn. One
  shared reader for yes/no/which-one answers (`cygnus/brain/consent.py`).
- **Phone**: the HUD is served over Tailscale, so the phone can talk to
  Cygnus, see what is running, and bring a window forward.

## Running it

**New here? [docs/SETUP.md](docs/SETUP.md)** walks a clean Windows laptop
through it in about twenty minutes, and
**[docs/WHAT_IT_CAN_DO.md](docs/WHAT_IT_CAN_DO.md)** says plainly what Cygnus
can do to a machine and what it always asks about first.
**[docs/PRIVACY.md](docs/PRIVACY.md)** answers the next question - what the
microphone does, what leaves the machine, what is written down, and how to
delete it - with the file and line for each answer.

Requirements: Windows, Python 3.10+, Node.js (for the desktop app), a
microphone (or `JARVIS_NO_VOICE=1` and type), and an API key for a model that
you make yourself - Google AI Studio (Gemini) is free and needs no card; see
**[docs/API_KEY.md](docs/API_KEY.md)** - or Ollama for local-only.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # then set CYGNUS_PROVIDER and CYGNUS_API_KEY
```

The backend and HUD (port from `JARVIS_PORT` in `.env`):

```bash
.venv\Scripts\python -m cygnus     # then open http://127.0.0.1:<port>
```

The desktop app in `desktop/` puts the HUD in a tray panel (Ctrl+Alt+J), a
full view and a mini orb:

```bash
cd desktop
npm install
npm run unpack-electron    # only if node_modules/electron/dist has no electron.exe
npm run dev
```

Cygnus switches between them when asked: "go full HUD", "full screen
yourself" (the full HUD over the taskbar too; Escape leaves it), "mini orb",
"back to the panel", "minimize yourself" (the full HUD minimizes to the
taskbar, the panel and the orb hide to the tray).

If a backend is already running, the app attaches to it; otherwise it starts
one from `.venv`, restarts it if it crashes, and stops it on quit. Do not run
`scripts/watch_and_restart.py` while the app owns the backend: both would
start a server on the same port.

## Building the installer

The installer carries its own Python and the backend, so the student's PC
needs nothing else: no Python, pip or npm. Installed, the code sits read-only
in the app's folder and the settings and data go to `%APPDATA%\Cygnus`; the
first start opens the setup screen.

```bash
.venv\Scripts\python scripts\build\build_backend.py   # desktop/build: Python, packages, code, wake-word model
cd desktop
npm install
npm run pack      # dist/win-unpacked: the app as a folder, to try without installing
npm run dist      # dist/Cygnus Setup <version>.exe
```

`build_backend.py --code` refreshes only the code (seconds) when no package
changed. The build needs `uv` on PATH and the wake-word model in
`data/models/kws` (docs/SETUP.md). The installer is not signed yet, so Windows
SmartScreen warns on it (docs/plans/DISTRIBUTION_PLAN.md).

## Routine check

One command runs every check and writes `data/logs/prod-check-<time>.json`:
compile, the pytest suite, the routing gate, the running server (9router up
and answering, at least three healthy cloud models, not on the local model),
and the smoke test against it.

```bash
.venv\Scripts\python scripts\prod_check.py              # safe: nothing on the machine changes
.venv\Scripts\python scripts\prod_check.py --tier live  # also opens/closes apps, tabs, music
.venv\Scripts\python scripts\prod_check.py --no-server  # code checks only
.venv\Scripts\python scripts\prod_check.py --bench      # and the measurements, against their targets
.venv\Scripts\python scripts\prod_check.py --bench --chaos   # and the fallback suite
```

`--bench` adds the routing gold set (`scripts/bench/routing.py`), grounding,
latency, two clients at once (`concurrency.py`) and the page audit; `--chaos` adds the fallback suite, which starts and
stops its own Cygnus. The targets are in `docs/plans/OPEN_SOURCE_PLAN.md`.

A run that takes hours - the soak, say - should not be a background job of a
terminal, because it dies with it:

```bash
powershell -ExecutionPolicy Bypass -File scripts\windows\run_detached.ps1 `
    -Name CygnusSoak -Script scripts\bench\soak.py -Args "--hours 24"
schtasks /end /tn CygnusSoak     # stop it; then /delete /tn CygnusSoak /f
```

Run it before a push and after each restructure step. Exit code 0 means
everything that ran passed.

### Incident watcher

`scripts/watch_incidents.py` follows every turn (`data/logs/turns.jsonl`) and
the backend's log while Cygnus runs, and writes what goes wrong to
`data/logs/incidents.jsonl`: a turn no model answered, a model that stopped
mid-turn, an error shown as the reply, a crash, a slow turn, a backend that
stopped answering. Errors also show a Windows notification.

Rules cannot see a turn that went wrong while looking right ("play woman by
harry styles" played another song and said it had played it). Once an hour,
`scripts/review_incidents.py` gives the new turns to Claude through `claude -p`
and adds what it finds to the same file, with one notification per review.
That sends your words to Anthropic under your own login: see
[docs/PRIVACY.md](docs/PRIVACY.md).

```bash
# The watcher at every logon and the hourly review (-NoReview: the watcher only)
powershell -ExecutionPolicy Bypass -File scripts\windows\install_incident_tasks.ps1
powershell -ExecutionPolicy Bypass -File scripts\windows\install_incident_tasks.ps1 -Remove
.venv\Scripts\python scripts\watch_incidents.py --summary      # the last day, by kind
.venv\Scripts\python scripts\review_incidents.py --dry-run     # what the next review would send
```

A Windows scheduled task, "Cygnus prod check", runs the safe tier every day
at 09:00 (or at the next wake if the laptop was asleep). It runs hidden; the
printout goes to `data/logs/prod-check-last.txt`, and a failed check keeps its
last 80 lines of output in the JSON report. Remove it in Task Scheduler.

## Settings

All settings live in `.env` (see `.env.example`). The ones most often changed:

| Setting | What it does |
|---|---|
| `CYGNUS_PROVIDER`, `CYGNUS_API_KEY`, `CYGNUS_MODEL` | Which model provider, your key for it, and optionally the model ([docs/API_KEY.md](docs/API_KEY.md)) |
| `ROUTER_BASE_URL`, `ROUTER_MODEL`, `ROUTER_FALLBACK_MODELS` | Written out, these override what the provider fills in: the endpoint and the models tried in order |
| `JARVIS_USE_OLLAMA`, `OLLAMA_MODEL` | The local fallback model |
| `WHISPER_MODEL` | Speech-to-text model size |
| `JARVIS_CLI_JOB_TIMEOUT` | How long a delegated coding job may run (seconds) |
| `JARVIS_IDLE_RESET_MINUTES` | Clear the conversation after this long idle (not while a task runs) |
| `JARVIS_PORT` | The HUD and backend port |

## Layout

| Path | What it is |
|---|---|
| `cygnus/server/` | FastAPI backend (`python -m cygnus`): `app.py` WebSocket turns, HUD, Running endpoints; `apps_routes.py` the Apps panel; `speech.py` TTS |
| `cygnus/brain/` | The model chain (`core.py`), intents, prompts, tool schema, the agent loop, 9router health and keeper |
| `cygnus/tools/` | Tools: apps, sites, music, delegation, files (`__init__.py`); the vault, phrase helpers, memory |
| `cygnus/safety.py` | The one confirm gate in front of every action |
| `cygnus/apps/` | Installed apps, their abilities, what is running |
| `cygnus/rules/` | The Apps-panel rules: compile, match, simulate, run |
| `cygnus/voice/`, `cygnus/memory/`, `cygnus/browser/`, `cygnus/jobs/`, `cygnus/agents/` | Speech and wake word; memory and sessions; the browser; background jobs; music, reports, projects |
| `cygnus/paths.py` | Where every file lives |
| `web/` | The HUD, the Apps panel and their static files |
| `config/` | Committed settings: app aliases, agents, projects |
| `desktop/` | Electron desktop app |
| `eval/` | The routing gate and evaluations |
| `tests/` | pytest suite (`.venv\Scripts\python -m pytest`) |
| `scripts/` | The routine check, smoke tests, dev tools, `checks/`, `windows/` (PowerShell helpers) |
| `scripts/bench/` | What is measured: grounding, latency, chaos, the pages, a soak |
| `data/` | Everything Cygnus writes (never committed): memory, sessions, logs, scans |
| `docs/plans/`, `docs/reviews/` | Design notes and decisions for each feature; reviews |
| `scripts/prod_check.py` | The routine check (see above) |

## Author

Roger Abay Jr
