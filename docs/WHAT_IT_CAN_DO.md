# What Cygnus can do to your machine

Read this before you leave it listening. It is written so you can decide, not
to reassure you.

## It can, on its own

- **Open apps and websites** that are installed or registered, and bring a
  window that is already open to the front.
- **Read a window's text**, and take a picture of one when you ask it to
  describe what is on screen.
- **List what is running**: apps, servers, background programs, coding agents.
- **Read files** under your Documents, Desktop, Downloads, and the folders it
  is told about, and **write new files** there.
- **Search the web**, and open pages in your browser.
- **Play and pause music** in Spotify or a browser tab.
- **Remember facts about you** that you tell it to remember, in `data/`.
- **Hand a job to a coding agent** (Claude Code, Codex, Gemini CLI, Hermes)
  when one is installed - and that agent can change files.

## It asks first, every time

The gate is in `cygnus/safety.py`, at the point where an action runs, so it
holds no matter which part of Cygnus decided on it. A "yes" only counts in a
later turn than the question, so a model cannot answer for you.

- deleting, wiping, formatting, killing a process
- force-closing an app (unsaved work is lost)
- opening a program or script by its path (`.exe`, `.bat`, `.ps1` ...)
- replacing a file that already exists
- typing or pressing keys **in a terminal** - which is running commands
- sending your words to Hermes, or any agent, when the request could change
  things
- anything a coding agent would do to files

Say "no" and it drops the action.

## It refuses outright

- Writing outside the allowed folders (Documents, Desktop, Downloads, your
  vault if you have one, and the Cygnus folder itself).
- Anything touching credentials, payments or system dialogs through desktop
  control, even with a confirm.

## Off until you switch it on

- **Abilities that type into a terminal.** They arrive switched off. Turn one
  on in the Apps panel, and it still asks before each run.
- **The microphone**, until you click "Enable mic" in the HUD or the browser
  grants it.
- **Reading your browser history** to learn the sites you use often
  (`JARVIS_SCAN_BROWSER_HISTORY=1`). The Apps panel can still rescan when
  you ask it to.
- **The phone HUD.** It is only reachable if you expose the port yourself
  (the author uses Tailscale). From the phone, stopping a program is refused.

## What leaves your machine

- **Your speech does not.** It is transcribed on your laptop by Whisper.
- **The words of a turn do**, with the context that turn needs (recent
  conversation, your profile if you keep one, the names of apps involved), to
  whichever model endpoint you set in `.env`.
- **The text of every spoken reply does too**, to Microsoft, because the
  default voice is their cloud one (`edge-tts`). That includes anything Cygnus
  reads back to you from your files. Install piper or kokoro and set
  `JARVIS_TTS_ENGINE` for an offline voice; `JARVIS_LOCAL_ONLY=true` refuses
  the cloud voice rather than quietly using it.
- A local model alone is **not** enough to keep everything in: this page used
  to say "with Ollama and `JARVIS_PREFER_LOCAL=true`, nothing leaves at all",
  which was wrong while the voice was still Microsoft's. See
  [PRIVACY.md](PRIVACY.md).
- **A picture of a window** leaves only when you ask Cygnus to describe one,
  and it says so in its reply.
- **Search terms** go to the search engine, as they would in a browser.

For the whole picture - the microphone, the logs that hold your words, what
is kept forever and how to delete it - see **[PRIVACY.md](PRIVACY.md)**.

## What it keeps, and where

Everything under `data/`, which is never committed:

| Path | What |
|---|---|
| `data/sessions/` | Every turn you have said and its reply |
| `data/memory/` | Facts you asked it to remember, and their index |
| `data/logs/audit.jsonl` | Every tool call: what ran, with what, and whether you confirmed |
| `data/app_registry.json` | The apps found on this machine |
| `data/web_registry.json` | Sites you visit often, from your browser history |
| `data/jarvis-profile.md` | The profile you wrote about yourself |

Delete any of it whenever you like; Cygnus builds what it needs again.

## When it cannot answer

It says so, and why: the endpoint is unreachable, the quota is out (with the
reset time), the model did not answer in time, or it answered with nothing.
It does not invent an answer, and it never claims to have done something it
did not do - `scripts/bench/grounding.py` measures exactly that.
