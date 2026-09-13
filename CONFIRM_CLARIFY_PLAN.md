# Smart confirm and clarify: plan

Status: approved 2026-09-13 with these decisions:
- a safe but unsure request is acted on, saying what it does; it asks only
  when there are several real choices
- all phases are built, but the decision point starts in shadow:
  `JARVIS_GATE=shadow` (default) only logs to `logs/gate_shadow.jsonl`; after a
  day of normal use the thresholds are tuned and `JARVIS_GATE=lead` turns it on
- confirms stay answerable from any of the owner's devices

## Goal

Cygnus acts when it is sure and the action is safe, reads the action back and
asks for a confirm when it is risky, and asks what was meant when it is unsure,
instead of guessing. One question at a time, in plain spoken words, with the
answer understood the same way everywhere.

## Where things stand

Already done (commits `9599ee5`, `bf47d6f`, `e1dcddd`):
- confirm latches expire with their job card (10 min); a late "confirm" is told so
- "yeah", "sure, go ahead", "go for it", "oo", "sige" confirm; "hindi", "huwag" decline
- a possible preference is asked about ("Should I remember that ...?") instead of saved

Still true today:
- **Uncertain requests are never asked about.** In `_think_turn`
  (`brain_gemini.py:2202-2263`) the router steps aside below 0.85 confidence
  (`intent_router.route`, line 241) and the turn falls straight into
  `run_autonomous` or `tools.delegate`, which run it or ask only for a confirm.
- **The semantic "clarify" route says "Sorry, I didn't catch that"**
  (`brain_gemini.py:2120`) and keeps no state, so the answer is a fresh turn.
- **"Did you mean ...?" (intake) runs on web voice only** (`jarvis_web.py`,
  Layer 1), never on typed text or the phone, gets no Whisper word
  confidence, and records no state for the reply.
- **Pending questions live in several places**: `dialogue_state`,
  `conversation_window` (app-or-site), `tools._PENDING_RULE_SLOT` and
  `rules_steps`, the confirm latches, and the last assistant message
  (`OPEN_CLARIFY_Q`). `[NEEDS_PICK]` has no resume handler.
- **Yes/no is read by several regexes** (brain, intent_router, tools
  preference, rules_steps) that still differ a little.

## The design

### 1. One decision per request: act, confirm or clarify

A new module `action_gate.py` answers one question for a request that is about
to act: `decide(text, signals) -> Decision(kind, question, options, readback)`.

It weighs two things it already has the signals for:

| | Sure what was meant | Unsure what was meant |
|---|---|---|
| **Safe** (look, open, play, search) | act | act and say what it's doing; ask only if there are several real choices |
| **Risky** (changes files, installs, sends, deletes, runs an agent that edits) | read back exactly what will happen, then confirm | ask what was meant first, then read back and confirm |

**Risk** comes from what exists today: `tools._is_safe_task`, `_MUTATING_RE`,
`_DESTRUCTIVE_RE`, the hard blocks, and an ability's `level`.

**Sure / unsure** combines, per request:
- heard: `transcript_pick` says "unsure", or a Whisper word below 0.5 (voice only)
- understood: router confidence 0.5-0.85 with more than one candidate, a
  semantic pick between 0.6 and 0.7, an intake entity scored "low", or a
  target that resolves to several apps or sites
- complete: a needed detail is missing ("play" with no song, "send it" with no "it")

### 2. Where it runs

In `_think_turn`, after the check-in and semantic lead and before the router
lead (`brain_gemini.py:2202`), for turns that are about to act (not chat, not
questions). The model's own tool calls keep today's gate in `execute_tool`
(consent policy and confirm latches). The intake "did you mean" check moves
into the same place, so typed text and the phone get it too; typed text is
never "misheard", so only the understood and complete signals apply there.

### 3. Questions

- **Clarify** names the choices, at most three: "The Spotify app or the website, sir?"
- **Confirm** reads back the action, not the raw words: "I'll ask Claude to
  edit jarvis_hud_v3.html in Cygnus's folder. Confirm?"
- **Missing detail** asks for just that: "Which song?"
- One open question at a time, never the same question twice in a row, and a
  question expires after 3 minutes (confirms after 10, as now).

### 4. One place for open questions

`dialogue_state` becomes the only store. Each question has a kind (confirm,
clarify, pick, slot, remember, preference, ability), its options, what to run
on each answer, a timeout, and the device that asked. The app-or-site
question, the rule slot, `OPEN_CLARIFY_Q` and `[NEEDS_PICK]` move into it; the
confirm latches stay in `tools` but register their question here.

### 5. One reader for replies

`consent.py` reads a reply to the open question as one of: yes, no, pick
(by number, name or "the first one"), change ("no, make it the website"),
cancel, or not an answer (a new request, routed normally). Every question
kind uses it, replacing the separate yes/no regexes. A bare "ok" stays an
acknowledgement, never a yes to a risky confirm.

## Phases

Each phase ships with tests, keeps the full suite green, and is its own commit.

Built 2026-09-13: phases 1-2 in `fc800d0`, phases 3-5 in the commit after it.
The gate runs in shadow. To let it ask, set `JARVIS_GATE=lead` in `.env` and
restart the backend; `JARVIS_GATE=off` turns it off.

1. **Reply reader** (`consent.py`) and the existing questions moved onto it. No
   behaviour change beyond consistent yes/no/pick.
2. **One question store**: app-or-site, rule slot, `OPEN_CLARIFY_Q` and
   `[NEEDS_PICK]` move into `dialogue_state`; picks get a real resume.
3. **Decision point in shadow**: `action_gate.decide` runs on every acting
   turn and logs what it would have done to `logs/gate_shadow.jsonl` next to
   what actually ran. Nothing changes for the user. Run it for a day of normal use.
4. **Decision point leads**, tuned on the shadow log: clarify when unsure,
   read-back confirm when risky, intake on every input path.
5. **Read-backs**: confirms describe the concrete action (tool, target, scope)
   from the gate instead of repeating the request.

Measure from the shadow log before phase 4: how often it would ask (target:
under 1 in 10 safe requests), and how many of those the old routing got wrong.

## Not in this plan

- Per-device confirms (a phone confirm answered from the desktop is still
  allowed). See open decision 1.
- Changing what counts as risky beyond reading today's lists.
- Multi-step goals' own step confirms (`rules_steps`) beyond using the reply reader.

## Open decisions

1. **Per-device confirms**: keep cross-device answers, or each device answers only its own?
2. **How chatty**: for a safe but unsure request, ask (fewer wrong actions) or
   act and say what it's doing (fewer questions)? The plan says act unless there
   are several real choices.
3. **Shadow period**: one day of normal use before phase 4, or go straight to leading?
