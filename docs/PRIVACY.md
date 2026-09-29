# What Cygnus knows about you

Cygnus listens to a microphone, reads your files, watches what is running and
can hand work to a coding agent. So this page answers four questions plainly:
what the microphone does, what leaves your machine, what is written down, and
how to delete it.

Written 2026-09-23 by reading the code, not the intentions. Every claim below
names the file it comes from, so you can check it. Where something is worse
than you would hope, it says so.

---

## The microphone

- **Your audio is transcribed on your laptop**, by Whisper (`faster-whisper`).
  It is not uploaded to anyone. That is true of both the wake word and your
  commands.
- **Nothing listens until you turn the mic on** in the HUD, or run the desktop
  app, which does it for you.
- **Recordings are not kept.** A command clip becomes a temporary `.wav`
  (`cygnus/server/app.py:399`) and is deleted after transcription
  (`app.py:1139`). The wake listener never writes audio at all - it holds a
  2.5-second rolling window in memory (`cygnus/voice/wake.py`).
  - Known flaw: that `unlink` is not in a `finally`, so if transcription
    raises, one `.wav` of your voice can be left behind in `%TEMP%`.
- **The wake listener writes down what it hears**, as text, to
  `data/logs/wake_debug.log` (`cygnus/voice/wake.py:374`), on by default.
  With the keyword spotter it logs only what the spotter hears (a wake, or
  one it ignored), not the empty windows. If the
  spotter fails to load, Whisper transcribes each window instead and **every
  2.5 seconds of speech near the mic is written to that file**. It stays on
  your disk, it rotates at 1 MB with one backup - and it is still a running
  transcript of your room. Turn it off with `JARVIS_WAKE_DEBUG=0`.
- `scripts/voice_testset_record.py` records permanent `.wav` files of your
  voice under `eval/voice_testset/` (and, with `--set wake`, under
  `eval/voice_wakeset/`, where the music clips also keep a recording of what
  your speakers played). It only runs when you run it. Both folders are
  git-ignored.
- **Your voiceprint.** `scripts/voiceprint_enroll.py` turns your enrollment
  recordings into a voiceprint: a list of 192 numbers describing your voice,
  not audio, in `data/voiceprint.json` (git-ignored, never uploaded). With it,
  the wake listener compares each "Cygnus" it hears with it and ignores other
  voices (`cygnus/voice/voiceprint.py`). It is biometric data about you: delete
  it with `python scripts/voiceprint_enroll.py --delete`, or turn the check off
  with `JARVIS_VOICEPRINT=off`. It is not security: a recording of your voice
  passes it.

## What leaves your machine

Nothing goes to the author of Cygnus. There is no telemetry, no analytics, no
crash reporting, no update check, no licence call - checked across `cygnus/`,
`web/` and `desktop/`. The destinations below are your own endpoints, services
you asked for, or asset CDNs.

| What | Where | When |
|---|---|---|
| Your words, recent conversation, your profile, tool results | the model endpoint **you** set in `.env` (`ROUTER_BASE_URL`; 9router is a local proxy that forwards to the provider of the model you chose) | every turn that needs a model |
| **The text of every spoken reply** | **Microsoft**, via `edge-tts` (`cygnus/server/speech.py:174`) | every spoken reply, by default |
| Search terms | Google / YouTube / Spotify, in your browser | you ask it to search |
| The pixels of one window | your model endpoint, as an image | only when you ask it to describe a window (`cygnus/apps/running.py:752`) |
| A city name | `wttr.in` | you ask for weather |
| 12 KB slices of recent conversation | your model endpoint | every 6 hours, to distil memories (`cygnus/memory/hygiene.py:141`) |
| Page fonts | `fonts.googleapis.com`, `fonts.gstatic.com` | every time the HUD loads |
| One-off model downloads | `huggingface.co` | first use of vault or semantic search |
| Your prompt, under your own logins | manus.im, chatgpt.com, claude.ai, copilot.microsoft.com | only via the opt-in browser-agent tools |
| Your words and Cygnus's replies from the desktop app, browser tabs and the phone (600 characters each at most), with the backend and tools of each turn | **Anthropic**, through Claude Code (`claude -p`) under your own login (`scripts/review_incidents.py`) | every hour, only if you installed the review task (`scripts/windows/install_incident_tasks.ps1`; `-NoReview` leaves it out, `-Remove` takes it away) |

**The voice is the surprising one.** The default TTS is Microsoft's cloud
voice: the full text of every reply is uploaded to speak it - including
anything Cygnus reads back to you from your files, your vault or your memory.
For offline speech, install piper (`~/.jarvis-tts`) or kokoro
(`models/kokoro`) and set `JARVIS_TTS_ENGINE`. Note that both **fall back to
edge-tts if their model file is missing**, so check the log line once.

`JARVIS_LOCAL_ONLY=true` now refuses the cloud voice outright rather than
falling back to it: you get no audio instead of a silent upload
(`cygnus/server/speech.py:174`). Before 2026-09-23 it did not, and
`WHAT_IT_CAN_DO.md` claimed "nothing leaves at all" while the voice was
shipping every reply to Microsoft. That was wrong, and it is fixed.

Coding agents (Hermes, Claude Code, Codex, Gemini CLI) are **local programs**.
Cygnus hands them the task text. What they then send, and to whom, is their
business, not Cygnus's - they have their own accounts and their own network
behaviour.

## What is written down, and where

Everything below is under `data/`, which is never committed, unless the row
says otherwise. `CYGNUS_DATA_DIR` moves the lot somewhere else.

| Path | What is in it |
|---|---|
| `data/sessions/*.jsonl` | Every turn, yours and its, verbatim, one file per day (`cygnus/memory/session_store.py:53`) |
| `data/sessions/index.db` | The same text again, indexed for search |
| `data/memory/semantic.db` | Facts you asked it to remember, and facts distilled from your conversations |
| `data/jarvis-profile.md` | Your profile, and up to 40 remembered facts |
| `data/logs/audit.jsonl` | Every tool call: what ran, the arguments, the result, whether you confirmed. **Contains what you said** |
| `data/logs/shadow.jsonl` | Routing research: your utterance, the reply, and several previous turns |
| `data/logs/jobs.jsonl` | Every background job and the goal you gave it |
| `data/logs/turns.jsonl` | Every turn: what you said and the reply (first 600 characters each), which model answered, how long it took (`cygnus/incidents.py`) |
| `data/logs/incidents.jsonl` | Turns and log lines the incident watcher and the hourly review found going wrong, with **what you said** and the log lines before it (`scripts/watch_incidents.py`, `scripts/review_incidents.py`) |
| `data/logs/desktop-backend.log` | The backend's console output, which includes `[STT] raw: '<what you said>'` and tool arguments |
| `data/logs/wake_debug.log` | What the wake listener heard (see above) |
| `data/web_registry.json` | Sites you visit often - host, visit counts, **and your five most-visited URLs per site** |
| `data/app_registry.json` | Every program installed on the machine, with full paths |
| `~/Documents/JARVIS_screenshot_*.png` | Full-screen captures, when you ask for one. **Outside `data/`** |
| `~/.jarvis-chrome-profile/`, `~/.jarvis-brave-profile/` | Full browser profiles, with cookies, for the automation |

**There is no retention policy.** Only two things are bounded: the wake log
(1 MB, one backup) and the remembered-facts list (40, the overflow archived
rather than deleted). Sessions, the audit log, the shadow log, the turn and
incident logs and the backend log grow forever. On the author's machine after a month: 7.2 MB of backend log
holding 139 lines of verbatim speech, 2 MB of shadow log, 1.5 MB of audit log.

That is a real gap, not a design. It is
[tracked](plans/OPEN_SOURCE_PLAN.md) and not yet fixed.

## Deleting it

There is no "forget everything" command yet. What there is:

```bash
# All of it. Cygnus rebuilds what it needs.
rm -rf data/sessions data/memory data/logs data/web_registry.json

# Just the transcript of what you have said
rm data/sessions/*.jsonl data/sessions/index.db

# Just the logs that hold your speech verbatim
rm data/logs/desktop-backend.log data/logs/wake_debug.log data/logs/turns.jsonl data/logs/incidents.jsonl

# The browser profiles Cygnus drives
rm -rf ~/.jarvis-chrome-profile ~/.jarvis-brave-profile
```

Closing Cygnus first is tidier, but none of it is load-bearing.

## Turning things off

| Setting | Effect |
|---|---|
| `JARVIS_NO_VOICE=1` | No microphone, no Whisper, typed turns only |
| `JARVIS_WAKE_DEBUG=0` | The wake listener stops writing down what it heard |
| `JARVIS_SCAN_BROWSER_HISTORY=0` (the default) | Your browser history is not read |
| `JARVIS_TTS_ENGINE=piper` or `kokoro` | Speech is synthesised locally, if the model is installed |
| `JARVIS_LOCAL_ONLY=true` | No cloud model, no cloud voice |
| `JARVIS_USE_OLLAMA=true` + `JARVIS_PREFER_LOCAL=true` | The brain answers from a model on your machine |

With `JARVIS_LOCAL_ONLY=true`, a local model, and a local voice installed,
nothing you say or hear leaves the machine. Search, weather and the browser
tools still reach the internet when you ask them to - they are requests to the
internet, which is the point of them.

## What it never does

- No telemetry, analytics, crash reports or update pings, to anyone.
- No keylogger: Cygnus sends keystrokes to apps, it does not record yours.
- No screen recording: single screenshots only, and only when asked.
- No clipboard history: it reads the clipboard only inside one copy action,
  and puts back what was there.
- Nothing is written to your Obsidian vault by the vault tools - they are
  read-only (`cygnus/tools/vault.py`). The general "write a file" tool *can*
  write there, if you ask it to.

## If you find this page wrong

It was written by reading the code, and code changes. `scripts/bench/` has the
measurements; `data/logs/audit.jsonl` is the record of what actually ran on
your machine. If the page and the code disagree, the code is right and the
page is a bug - please report it.
