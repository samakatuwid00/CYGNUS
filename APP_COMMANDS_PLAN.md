# App commands: "/app instruction" from the Running panel

Owner's idea (2026-09-15): what is running can be used, not only seen. Typing
`/hermes start a new session then prompt: what is the status of second brain,
then ask hermes to delegate it to claude` makes Cygnus carry that out in the
Hermes that is running, the way it runs: a desktop app through its window, a
command-line agent through its terminal.

## Decisions (owner, 2026-09-15)

1. Every running row Cygnus can drive gets a command: agents and apps.
2. The command targets the running instance, not the product. Hermes open as
   the desktop app and as a CLI are two entries (`/hermes`, `/hermes-cli`),
   so Cygnus knows which one to use. A GUI app is driven through its window;
   a CLI through its terminal.
3. More than one step shows the steps first (Run / Edit / Cancel); a single
   step just runs. Anything that may change files still asks (consent.py).
4. Works from the phone too, window driving included (it moves windows on the
   laptop screen).

## What exists (research 2026-09-15)

- The HUD's input already takes `/clear`, `/reset`, `/redirect`
  (`sendTyped`, jarvis_hud_v3.html); anything else is "UNKNOWN COMMAND". The
  WebSocket has a `type:'command'` message. No suggestion list.
- Running rows carry `id`, `group`, `name`, `doing`, `pids`, `windows[{hwnd,
  title}]`, `exes` (running_apps.build_rows). Hermes desktop app, Hermes CLI,
  Claude Code, Codex, Gemini are agent rows; windowed programs are app rows.
- Window driving: app_abilities (abilities in app_registry.json, UI
  Automation deep scan, `run_ability` with click/type/key/focus steps),
  desktop_driver (find_window, click_control, type_into_control), rules_steps
  (launch/click/type/key/wait/ask across turns). Hermes has no abilities: its
  deep scan found 0 controls.
- Reading back: running_apps.window_text (UI Automation text; terminals work)
  and describe_window (picture to a vision model).
- Hermes headless: warm_harness.warm_send(fresh=True) = new session;
  warm_redirect = `--resume` into it. Hermes can delegate to Claude
  (claude-code-orchestration skill) but has sometimes only described it.
- command_chain splits "X then Y" but routes each clause on its own, with no
  addressee carried through.

## Design

**One entry per running instance.** The `/` list is built from Running rows
that have a driver:

```
/hermes       Hermes · desktop app       window
/hermes-cli   Hermes · terminal          terminal
/claude       Claude Code · jarvis-demo  terminal
/powerpoint   PowerPoint · User Guide    window
```

The short name has a suffix only when two instances would share it.

**Two drivers, chosen by how the instance runs:**

| Instance | Drive | Read |
|---|---|---|
| Window (Hermes app, PowerPoint, Chrome) | raise it; click / key / type or paste via UI Automation or the app's abilities; keyboard recipes where it exposes no controls | window text, else a picture described by the vision model |
| Terminal (Hermes CLI, Claude Code, Codex) | raise its terminal; paste the prompt; Enter | the terminal's text until the agent is done |
| Not running | offer to start it | — |

**Steps.** Each driver declares a small vocabulary: new session, send,
wait, read, show. "start a new session then prompt X then ask it to
delegate to claude" becomes steps for that one instance: plain phrasing by
rule, the rest by the model restricted to that vocabulary. Steps run as one
task in the Running panel; answers go to the task and the conversation.

**Verification.** A step that hands work to another agent is done only when
that agent is seen running (running_apps), not when the reply says so.

## Phases

1. Spike: prove both drivers on safe targets (below), then on Hermes.
2. `/` list: slug + driver per row in `/running`; suggestion list in the
   input (laptop and phone); `{type:'app_command', row, text}` over the
   WebSocket.
3. Terminal driver: raise, paste, Enter, read until done. `/claude`,
   `/hermes-cli`.
4. Window driver: abilities or keyboard recipes, picture check per step;
   "learn this app" (the owner shows it once, the steps are saved).
5. Step planner and preview card.
6. Hand-off verification, follow-ups into the same session, phone.

## Spike (phase 1)

Safe targets only: a new Notepad window and a new terminal opened for the
spike. The owner's Hermes app is inspected read-only (which controls it
exposes); nothing is typed into it and no session is started without asking.
Claude Code sessions are not typed into (one of them is the session doing
this work).

Results: see "Spike results" below.

## Spike results (2026-09-15)

- **Terminal driver works.** A new Windows Terminal window (`wt -w new`)
  was raised, `echo cygnus-spike-36048` pasted and entered, and the command
  and its output read back through UI Automation in 2.0 s. The clipboard was
  saved and restored.
- **Window paste works without UI Automation.** A Tk window whose controls
  UI Automation cannot see got the pasted text and the Enter in 2.0 s: raise,
  check it is really in front, Ctrl+V, Enter.
- **Keys go only to the window in front.** The driver must check the target
  is the foreground window before every paste and Enter; otherwise the keys
  land in whatever has focus (the owner's app, or a Claude Code terminal).
- **Hermes (Electron) exposes its UI, but only after the first query.** The
  first query saw 13 elements (window buttons); 3 s later 312: 88 buttons,
  18 list items, the transcript as text, the **Message** edit box, the
  **Model** picker, "Branch in new chat", "Show sidebar · N unread
  sessions". Chromium builds its accessibility tree once a client asks, so a
  driver asks, waits, then asks again. The earlier deep scan's "found 0" was
  the first, empty answer.
- **Hermes new session is Ctrl+N.** From its source
  (`apps/desktop/src/lib/keybinds/actions.ts:83`: `session.new` defaults to
  `mod+n`; `mod+t` new session tab, `mod+shift+n` new window). No clicking
  through the hidden sidebar needed.
- **Hermes recipe (from the spike, not yet run live):** raise the window,
  check it is in front, Ctrl+N, ask UI Automation twice until the Message
  box appears, focus it, paste, Enter, then read the transcript's text
  elements until the reply stops growing. No Send button was found; the
  composer sends on Enter like the rest of the app.
- **Live run in the owner's Hermes (approved):** raised, Ctrl+N, Message
  box found, prompt pasted and sent, "READY" read back 7.6 s after sending,
  11.5 s in all; clipboard restored. The first read also took the status bar
  ("7 t/s | RAM 14G/15G"); the driver now reads only text above the Message
  box.

## Version 1 (built 2026-09-15)

- `app_commands.py`: `targets()` (the "/" menu: one entry per running
  window, drivable first, `-2` for a second copy), `plan()` (clauses split on
  "then" / ";" outside quotes; "new session"; "prompt: X", "ask hermes to X"
  send X), `HermesWindow` (raise, check front before every key, Ctrl+N,
  Message box, paste, Enter, read until the reply is steady 3 s), `run()`
  (one command at a time; a failed step stops the rest).
- `jarvis_web.py`: `GET /app/targets` (phone too); WebSocket `app_command`
  (one step runs, more come back as `app_plan`), `app_run`, `app_cancel`
  (plans kept 5 minutes). The reply joins the client's conversation.
- HUD: typing "/" shows the menu (Tab completes; drivable entries marked
  WINDOW, others NOT YET); "/slug text" sends an app command; a plan shows
  its steps with RUN / CANCEL.
- Checked live: `/hermes Reply with just the word PONG` answered "PONG" in
  13.4 s; at phone width through ts.net the menu, Tab, the two-step card and
  Cancel worked with no page errors.

### Phone test fixes (2026-09-15)

The owner's phone test, "/hermes on the new session prompt what is the
status of musubae? show it on the screen too", pasted the whole line into
the open session, and the answer read back was Hermes's HTML artifact taken
apart token by token. Three fixes:

- **"new session" is heard wherever it is said:** at the start ("on the new
  session prompt X", "start a new session and ask X", "new session: X") or
  the end ("X in a new session"), not only as its own "then" step.
- **The reply is Hermes's own copy of it:** each finished reply gets Copy /
  Read aloud / Refresh buttons; one more "Read aloud" means Hermes is done,
  and pressing that message's Copy (through UI Automation, clipboard put
  back) gives the reply as written. Its `::preview{file=...}` marker becomes
  "(Hermes made x.html; it's open in Hermes.)".
- **A minimised Hermes is waited for:** it takes a moment to restore; the
  driver now waits up to 2 s (twice) for it to be in front instead of one
  0.4 s check.

Checked live with Hermes minimised: "on a new session prompt Reply with just
the word PONG" became New session + Send, and Run answered "Started a new
Hermes session. Hermes, to ...: PONG" in 10.0 s.

### Reading the intent (2026-09-15, owner: "not hardcoded")

The phrase rules above heard "new session" but nothing else: "on the
session X prompt Y" or "go to my musubae chat" went to Hermes word for word,
and "next slide" would have been typed into PowerPoint. Now:

- **The model reads the instruction** against the app's own actions and may
  use only those (`understand()`, rules_ai's router call). Hermes's actions:
  new session, open a session by title (the sidebar's titles are given as
  choices), send. Other apps: their abilities (all registry entries for the
  running program, e.g. powerpnt + powerpoint) and the owner's rules.
- **What it has no action for is said, not guessed:** a step naming an
  action that isn't listed counts as missing too. Cygnus runs nothing,
  names what the app can do (named actions; scanned buttons are only
  counted, since their names are the window's own text) and says to add a
  rule in the Apps panel.
- **Apps with abilities are drivable:** abilities and rules run through the
  voice router's own runners (`intent_router._run_ability` / `_run_rule`);
  an ability marked "ask" always shows the plan first.
- **Opening a Hermes session:** each session is a sidebar row named
  "Reorder <title> <title> …"; the spoken name is matched to a title by
  shared words and the row is invoked.
- **Focus:** if something else takes the focus before a key (Chrome did),
  Hermes is brought back once; a second loss stops.
- No model answering: Hermes falls back to the phrase rules; other apps say
  the model is needed.

Checked live: "/hermes go to the PONG session and say thanks" became Open
session "Reply with PONG" + Send, and answered "Anytime."; "/notepad next
slide" was refused with Notepad's actions and the rule hint, nothing run.

Not yet: terminal targets (CLI agents share Windows Terminal tabs, and one
is the Claude session doing this work: needs tab targeting first), other
apps' drivers, Edit on the plan card, a task card in the Running panel, and
checking that a hand-off really started the other agent.
