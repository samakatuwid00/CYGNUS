# Running now: what is on this machine, and what it is doing

Status: phases 1-3 built 2026-09-13 (`running_apps.py`, `GET /running`, the
HUD's Running Now section and Running tab, "what's running?" and "explain what
Claude is doing"). Phases 4-5 built the same day, the owner's "build now
what's not built yet" standing as phase 5's approval: window text, thumbnails,
"describe that window" (vision through the router: cx/gpt-5.6-luna,
cx/gpt-5.5, cx/gpt-5.6-terra, JARVIS_VISION_MODELS), bring to front, and stop
with a confirm; all laptop-only. A scan takes about 0.1 s once the first one
(about 6 s, done at backend startup) has read every command line.

## Goal

Cygnus shows what is running, the way Task Manager groups it, including the
programs with no window (a Claude CLI in a hidden terminal, an agent's
background run, a forgotten web server), and says in plain words what each
one is doing: working on what, waiting for the owner, idle, stuck, or a
leftover nobody needs. It only looks. Acting on anything (bring to front,
stop) is a later phase with its own approval.

## Why

A snapshot of this laptop on 2026-09-13, none of it visible on screen:
- two interactive Claude CLIs and three background `claude -p --model haiku` runs
- the Claude desktop app, the Hermes gateway and the Hermes desktop app (in duplicate)
- two pairs of `python -m http.server` on ports 8787 and 8788, apparently duplicates
- `tail`/`grep` log watchers left running since 2026-09-11

Task Manager lists them by name only; it cannot say what any of them is doing.

## What a program with no window can and cannot give

A process without a window has no screen to show. What it does leave:
its command line, its folder, its children, its CPU and memory, the ports it
listens on, and, for agents, the logs and transcripts they write. For a
Claude CLI that is nearly as good as its screen: its session transcript in
`~/.claude/projects/<folder>/<session>.jsonl` records the session's title
(`ai-title`), the last prompt (`last-prompt`), every tool it ran, and a
timestamp on every entry.

## Design

### 1. Inventory (`running_apps.py`)

`inventory()` lists what is running as rows, grouped like Task Manager:

| Group | What goes in it | How it is found |
|---|---|---|
| Apps | programs with a visible or minimized window | window list (title, owning process) |
| Agents | Claude CLI and desktop, Hermes, OpenCode, Gemini, Codex, and Cygnus's own tasks | process names and command lines, the jobs registry |
| Servers | anything listening on a local port | listening ports mapped to their process |
| Background | the rest worth knowing: long-running scripts, watchers, duplicates | process tree, minus Windows' own services |

Each row: name, process ids, window title if any, started when, CPU and
memory, ports, a short command line, a status, and one line of what it is
doing. Windows' own processes (svchost, services, drivers) are left out.
Needs `psutil` (not installed today) for process folders, CPU and memory
without spawning PowerShell every few seconds.

### 2. What each one is doing (readers)

| Kind | Source | Status it can give |
|---|---|---|
| Cygnus's own tasks | the jobs registry | exact: step, agent, waiting on a confirm |
| Claude Code CLI | its session transcript, matched to the process by folder | title, last prompt, last tool and file, last activity; **working** (last entry a tool call or a reply in progress), **waiting for you** (the reply ended), **idle** (nothing for 10 min) |
| `claude -p` background runs | command line (model, flags, prompt) and the parent that started it | what it was asked, who launched it (a hook, Cygnus, a script) |
| Hermes | `hermes/logs/agent.log` | current session, last step, idle since |
| Servers | port and command line | "web server on 8788 serving <folder>" |
| Apps with windows | window title | VS Code's project and file, the browser's page |
| Terminals (phase 4) | the window's text through Windows UI Automation (`pywinauto`, installed) | the last lines on screen |
| Anything else | name, CPU, memory, uptime | running, busy or quiet |

**Flags** on top of the status: **duplicate** (the same command line more than
once), **leftover** (running over a day with no CPU for an hour, like the
watchers since 2026-09-11), **stuck** (an agent alive with no activity for 30
min), **waiting for you**.

### 3. Showing it

- `GET /running` returns the rows (cached 5 s). The HUD polls it only while
  the section is visible.
- **Full HUD**: a "Running now" section next to Active Tasks, grouped Apps,
  Agents, Servers, Background, with a status dot and the one-line summary.
  Tapping a row opens its detail: a Claude session's last prompt and last
  steps, a server's folder, a terminal's last lines.
- **Panel view**: the same list, collapsed to Agents and anything flagged.
- No buttons that act in this plan.

### 4. Saying it

"Cygnus, what's running?" and "what is Claude doing?" route to a local
answer built from the rows, with no model call:

> "Two Claude sessions: one in jarvis-demo editing tools.py, active a minute
> ago; the other is waiting for your answer. Three quick Claude background
> checks started by a hook. Hermes is idle. Two duplicate web servers on 8787
> and 8788, and log watchers left running since Thursday."

A deeper "explain what Claude is doing" may hand the session's last steps to
the answering model; Cygnus says when that text leaves the machine.

### 5. Privacy and cost

- Everything above runs locally; the list and the spoken summary never
  leave the machine.
- Transcript and terminal text are read only for a row's detail or when asked.
- Screenshots and vision descriptions are not in phases 1-3; phase 4 makes
  them opt-in.
- Budget: the inventory under 300 ms; transcripts read from the last 64 KB only.

## Phases

Each phase ships with tests (fake process lists, sample transcript lines),
keeps the full suite green, and is its own commit.

1. **Inventory and panel**: `running_apps.inventory()`, `/running`, the Full
   HUD section and the Panel list. Adds `psutil`.
2. **Readers**: Cygnus's tasks, Claude sessions, `claude -p` runs, Hermes,
   servers, window titles.
3. **Voice and flags**: "what's running?", "what is Claude doing?",
   duplicate, leftover, stuck, waiting for you.
4. **Seeing more** (opt-in): terminal text through UI Automation, live
   window thumbnails in the HUD, and a vision description of a window on request.
5. **Acting** (separate approval): bring to front, stop a duplicate or a
   leftover, always behind the confirm step.

## Not in this plan

- Controlling any app, closing or killing anything (phase 5, later)
- Other users' processes and Windows services
- Recording the screen (screenpipe is installed but not running; not needed)

## Phases 4-5 as built (owner said "build now what's not built yet", 2026-09-13)

Phase 4, seeing more, each only when asked:
- **Window text**: a terminal's visible text through Windows UI Automation's
  TextPattern (Windows Terminal answers in about 0.1 s); other windows give
  their UI Automation text where they expose it.
- **Thumbnails**: the desktop app captures live window thumbnails
  (Electron's desktopCapturer) for the rows that have a window. The phone and
  a browser tab show none.
- **Describe a window**: the window is captured (PrintWindow, so it works when
  covered) and sent with a question to the answering model through the local
  router; Cygnus says the picture left the machine. No local vision model is
  installed, so this needs the router.

Phase 5, acting:
- **Bring to front** acts at once: it is harmless and undone by a click.
  (A deviation from "always behind the confirm step" above.) "Show X on
  screen", "put X on the screen", "show me X", "bring up X" mean the same, and
  so does "open X" when X already has a window: `open_application` raises it
  instead of launching a second copy (on 2026-09-14 "show on screen
  PowerPoint" from the phone opened a second PowerPoint beside the open
  deck). "Open a new X" / "another X window" still launches. When X is not
  running, Show opens it.
- **Stop** always asks first, naming the program, its folder or port and its
  process, and checks at the confirm that the process is still the same one
  (id and start time). A stop ends the process and its children, gently
  first, then forcibly after a few seconds.
- **What stop may touch**: servers and background programs. Never Cygnus
  itself, never an interactive Claude Code session, never a desktop app with a
  window (closing apps has its own tool), never Windows' own processes.

## Decisions (owner, 2026-09-13)

1. Vision descriptions of a window: allowed, only when asked for, never automatic.
2. "Explain what Claude is doing" may send the session's recent steps to the
   answering model; Cygnus says so when it does. "What's running?" stays local.
3. `psutil` may be added.
4. The phone's HUD shows the list too, read-only, like the desktop.
   Changed 2026-09-14: the phone may also bring a window forward (the Show
   button and "show X on screen"); read text, describe and stop stay
   laptop-only.
