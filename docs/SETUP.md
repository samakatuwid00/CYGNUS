# Setting Cygnus up on your own laptop

Written for a clean Windows 11 machine and someone who has not seen this code
before. About twenty minutes, most of it downloads.

Cygnus runs entirely on your machine. Your speech is transcribed locally; the
words of a turn, and the context it needs, go to whichever model you point it
at. Nothing else leaves the laptop.

## What you need

- **Windows 10 or 11.** The app control (opening apps, reading windows,
  typing into them) is Windows-only. The rest runs anywhere Python does.
- **Python 3.10 or newer** (`python --version`).
- **Node.js 18+**, only for the desktop app. The browser HUD works without it.
- **A microphone**, or `JARVIS_NO_VOICE=1` and type instead.
- **An API key for a model**, which you make yourself: Google AI Studio
  (Gemini) is free and needs no card. [API_KEY.md](API_KEY.md) walks through
  it and the other providers. Or [Ollama](https://ollama.com) for a local
  model, no key and no cloud.

## 1. Get the code and the dependencies

```bash
git clone <your fork or the repository>
cd CYGNUS
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
```

## 2. Settings

```bash
copy .env.example .env
```

Start Cygnus (step 3) and click **Set up**, or open
`http://127.0.0.1:8001/setup`: it checks what your PC can run, tries your key,
and restarts Cygnus with it. [API_KEY.md](API_KEY.md) shows how to get a key.

Or by hand: open `.env` and set the two lines at the top - the provider and
your key:

```ini
CYGNUS_PROVIDER=gemini
CYGNUS_API_KEY=AIza...your key...
```

`groq`, `openrouter` and `openai` work the same way with their own key. For
local only, no key and no cloud, install Ollama, run
`ollama pull qwen3:4b-instruct`, and set:

```ini
CYGNUS_PROVIDER=ollama
```

Worth setting while you are in there:

| Setting | Why |
|---|---|
| `WHISPER_INITIAL_PROMPT` | The names you say - your projects, your apps, the people you mention. It is the single biggest win for speech accuracy. |
| `WHISPER_MODEL` | `small.en` is a good start; `medium.en` is better and slower. |
| `JARVIS_PORT` | Change it if something already uses 8001. |
| `JARVIS_VAULT_ROOT` | Only if you keep an Obsidian vault and want Cygnus to read it. |
| `JARVIS_NO_VOICE=1` | No microphone: typed turns only. |

## 3. Start it

```bash
.venv\Scripts\python -m cygnus
```

Then open `http://127.0.0.1:8001` (or your `JARVIS_PORT`). Type in the box at
the bottom; click **Enable mic** to talk. Say "Cygnus" to wake it.

The desktop app (tray panel, mini orb, Ctrl+Alt+J) is optional:

```bash
cd desktop
npm install
npm run dev
```

## 4. Your first five minutes

Type or say these, in order. They show what Cygnus is without touching
anything on your machine:

1. `what time is it` - answered locally, no model call.
2. `what apps do I have?` - the first scan of your installed software. It
   takes a moment and only reads.
3. `open notepad` - it opens, or brings forward what is already open.
4. `what's running?` - the panel of what is on this laptop right now.
5. `delete the file C:\nothing\here.txt` - it asks first, and "no" cancels
   it. Nothing is deleted. This is the gate in front of every risky action.

Then open the **Apps panel** (the grid icon) and teach it one rule of your
own: pick an app, write what you would say, and what should happen.

## Where your things live

| Path | What |
|---|---|
| `.env` | Your settings and keys. Never committed. |
| `data/` | Everything Cygnus writes: memory, sessions, logs, the app scan, your profile. Never committed. |
| `config/` | Small shared settings and examples (app aliases, agents, example projects). |
| `data/projects.json` | Your own projects, seeded from `config/projects.example.json`. |

Point `CYGNUS_DATA_DIR` somewhere else if you would rather keep `data/` off
the repository folder.

## If something does not work

| What you see | What it means |
|---|---|
| "No API key is set for ..." | Click **Set up**, or fill `CYGNUS_API_KEY` in `.env`; see [API_KEY.md](API_KEY.md). |
| "The provider refused the API key" | The key is wrong or was deleted; copy it again or make a new one. |
| "I couldn't reach the model endpoint" | No internet, `ROUTER_BASE_URL` is wrong, or Ollama is not running. |
| "My model is out of quota" | The provider's limit; it says when it resets. |
| "No mic - click Enable mic" | The browser has not been given the microphone yet. |
| Port already in use | Another program has `JARVIS_PORT`; change it in `.env`. |
| Everything is slow | Check `/status`: if the brain says a local model, your endpoint is not answering. |

Run the checks any time:

```bash
.venv\Scripts\python scripts\prod_check.py          # compile, tests, routing, server, smoke
.venv\Scripts\python scripts\bench\ui_audit.py      # the pages, measured
```
