# Agent loop: the model first, with Cygnus's own tools, in a loop

Owner (2026-09-15): "why can't Cygnus be like you and Hermes, that can
delegate or understand the prompt?" Checked against the code, the answer is
wiring, not a missing feature.

## What Cygnus did with a sentence (before)

1. 76 phrase rules decide first (`classify_intent`, 30 intents), before any
   model reads the text.
2. 23 hand-written branches in `_think_turn` act on that label, one shot
   each: run, return.
3. Any command none of them claimed was handed to the Hermes CLI
   (`brain_gemini.py` ~2460), a separate process with no screen, its answer
   relayed word for word ("LOOK-ONLY: did not touch the Hermes desktop app",
   raw `<atem:function_calls>`, "ERIC — need one clarification").
4. Cygnus's own model-with-tools loop (10 rounds, 29 tools, 45 s) was last:
   in practice it got chat and questions, never commands.

Real agents run one loop, model first: read the request with the tools in
view, call one, read the result, decide again, for minutes. Phrase rules are
shortcuts on top.

## Design

`agent_loop.py`, behind `JARVIS_AGENT_LOOP` = `off` | `shadow` | `lead`.

- **Hook**: in `_think_turn`, after the answers to Cygnus's own questions
  (yes/no/confirm/pick) and the deterministic rule actions, before the
  phrase routing. Instant intents stay with the old code (time, math,
  greeting, thanks, help, music stop, job status/stop, remember/recall, the
  stop-a-program confirm flow, rescan, app install, sites registry, reports,
  project launch, bare-verb clarifies). Slash commands keep their own path.
- **Loop**: up to `JARVIS_AGENT_ROUNDS` (12) model calls under a budget of
  `JARVIS_AGENT_BUDGET` (240 s), each call capped at 45 s. After 8 s the
  progress callback says "On it, sir." so voice is not left silent. Models
  come from router_health's healthy list; a model that fails hands to the
  next. Everything failing returns None and the old routing takes the turn.
- **Tools** (14, small, each observed):
  whats_running, app_actions, app_step, show_window, read_window,
  describe_window, open_app, open_site, delegate (claude / codex / gemini /
  hermes, background, their own confirms), search_web, read_file,
  list_directory, recall, remember.
  `app_actions` + `app_step` are the app_commands actions (Hermes: new
  session / open session / send; other apps: abilities and the owner's
  rules). An ability marked "ask" is asked, not run.
- **Working memory**: the app in play per client, set by any app tool and
  shown to the model next turn ("try facebook.com" after "/chrome …").
- **Checking what comes back**: tool markup and questions addressed to
  someone else in a delegated agent's answer are cut and named as a broken
  answer; a reply claiming an action when no tool ran is replaced.
- **Consent**: the same policy as the old loop (`_apply_consent_policy`,
  `_held_for_question`): the model cannot grant itself confirm/overwrite;
  a "what happened?" question runs nothing.
- **Shadow**: one model decision per turn (no tool runs) written to
  `logs/agent_shadow.jsonl` next to what the old routing did, to compare.

## Live check (2026-09-15, lead)

This morning's failures, typed again with the loop leading:

| Said | Tools the loop chose | Reply | Time |
|---|---|---|---|
| display hermes | show_window | "Hermes is now displayed, sir." | 8.9 s |
| on the new session, prompt: what is the status of musubae? | app_actions, app_step new_session, app_step send | the Musubae status Hermes gave, in two sentences | 97 s (Hermes's own work; "On it, sir." at 8 s) |
| try facebook.com | open_site | "Facebook is open in Chrome, sir." | 12.8 s |

Before: sent to the Hermes CLI ("LOOK-ONLY: did not touch the app"),
raw tool markup relayed, and a page read instead of opened.

Shipped with `JARVIS_AGENT_LOOP=lead` in `.env`; `shadow` logs decisions
without acting, `off` restores the old routing entirely.

## Feedback and verification (2026-09-15, evening)

The loop acted without saying so, and once denied it. Now every acting
turn ends with a report built from the tool record, not the model ("I
started a new Hermes session; then sent 'X' to Hermes, which replied: …");
a closing line that contradicts the record is replaced by it; "status?" /
"what did you do?" are answered from the last task's record with no model
and no tools (no record: the old routing's job log); a failure after work
ends with the record. Sentences naming a running app or an agent belong to
the loop even when a phrase rule (time, stop, …) would claim them.

## Kept as they were

Slash commands, the confirm/clarify gate's questions and answers, the
running-stop confirm, rules with steps, music, wake, jobs.

## Not in this cut

Streaming partial replies; a visible task card for a long loop (the
progress line speaks instead); removing the old branches (they stay as the
fallback and for `off`).
