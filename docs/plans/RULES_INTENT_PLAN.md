# Rules that understand intent: setup, matching, and outcome

Owner's ask (2026-09-15): when a user sets a rule for any app, Cygnus must
read the meaning of what they said, propose a rule that does what they
expect, recognise that rule later from natural speech, and know whether it
did what the user wanted. Not only "which URL".

## What exists (research 2026-09-15)

- Authoring: `rules_voice.begin_rule_setup` -> AI compile (`rules_ai.compile_rule`)
  only when the phrase starts with search/find/open/play/watch/browse/show or
  has `->` / "when I say". Everything else goes to `rules_compiler.parse_scaffold`,
  a keyword parser that knows "explicit", a volume cap, and "never open/close/play".
  No model call on that path.
- Rule shape: type search_site | open_site | steps | block | reminder. No
  condition, no expected outcome. Blocks are absolute ("never open spotify after
  11pm" blocks always). Reminders only print a notice.
- Compile guards: a site the user never typed is dropped (so "open youtube
  music" asks "Which website?"); `rules_steps.SAFE_KEYS_RE` allows only
  Enter/Tab/arrows and ^a ^c ^f ^k ^l ^v ^x, so "press ctrl s" is dropped and
  the rule degrades to a reminder without saying so.
- Runtime: `rules_engine.match_action` is keyword-only: first word must equal
  the trigger's verb, all trigger words in order. "it's gym time", "sum up this
  column", "tell jd I'm late", "advance the slide" miss their rules. Slot
  trimming drops edge words ("message jd hello there" -> "hello").
- Only `/app` commands (`app_commands._PLAN_PROMPT`) read meaning with a model
  and offer rules as actions. Plain voice does not.
- Simulation (`rules_sim`): headless dump-DOM. Client-rendered pages (YouTube
  Music) always "FAILED" on click targets. Panel Test cannot answer the
  "Restart Brave?" / "Press play?" questions a steps rule asks (only voice can,
  tools.py:1701). `rules_steps._open` never sets `run["url"]`, so a later click
  hits the first tab. `browser_cdp._LOCATE_JS` matches substrings ("play" hits
  "New playlist").
- Desktop knowledge exists: scanned abilities and window controls in
  app_registry.json (`app_abilities`), running windows (`running_apps`), last
  result and open questions (`dialogue_state`), learned defaults (`preferences`).

## Design

**One intent, captured once, checked every time.** A rule is what the user
means, grounded in what Cygnus knows about the app, with a stated outcome.

```
rule = {
  rule_id, source_phrase, triggers[],
  goal:      "play my workout playlist on spotify",        # user's meaning
  app, condition: {time_after|time_before|app_state|null}, # when it applies
  action:    {type, site, steps[], slot, ...},             # as today
  expects:   "music is playing",                           # what done looks like
  gaps:      ["can't check the time yet"],                 # told to the user
  ...
}
```

**Setup (propose):** every phrase -> one model call with the app's real
context (category, abilities and controls, site catalog, existing rules,
preferences) -> plan uses real abilities/controls when they exist, resolves
known site names, asks one specific question only for a real gap, proposes in
the user's words with its gaps stated, verifies (web: headless; desktop:
control exists in scan; client-rendered: "unverified", never "failed"), user
refines or saves.

**Runtime (recognise):** fast keyword tier as today; on a miss and a
command-like utterance, one short model call over rule summaries plus focused
window and last result -> rule_id, slots, confidence. Low confidence asks
"Did you mean your gym time rule?"; a yes runs it and adds the phrase as a
trigger. Conditions checked before running.

**After (met expectation):** compare the result with `expects` (playback
confirmed, window/page reached). Mismatch -> say what happened, offer to
change the rule; the correction recompiles with feedback.

## Phases

1. **Fix what breaks today** (small, testable, no schema change)
   - `rules_steps._open` sets `run["url"]`; click targets the opened tab.
   - `_LOCATE_JS` word-boundary match.
   - `SAFE_KEYS_RE` grows to ^s ^z ^y ^p ^n ^o ^w ^t and Alt/F-keys; a step
     that is still dropped is reported in `gaps`, never silently.
   - Slot trimming keeps interior words ("hello there").
   - Simulation marks empty-DOM sites "unverified".
   - `describe_step("open")` says "open the site" when there is no pick.
   - Panel Test: `/apps/rules/test/answer` routes yes/no to `rules_steps.resume`;
     panel shows Yes / No buttons when the result is a question.

2. **Understand every phrase** (schema + compile)
   - Remove the `is_command_phrase` gate; scaffold parser becomes the
     no-model fallback only.
   - Prompt returns goal, condition, expects, gaps; `_clean_steps` keeps
     dropped steps in `gaps`.
   - Site catalog (`web_registry.json` + a short list of well-known names);
     a model site is accepted when the catalog knows it.
   - App context in the prompt: abilities, scanned controls, existing rules.
     Desktop click targets must match a control or ability, else a gap.
   - Block/reminder rules carry `condition`; `rules_engine.check` evaluates
     time and app-state conditions.
   - Proposal text lists gaps ("I can't check the time yet").

3. **Recognise from natural speech** (runtime)
   - `rules_engine.match_action` unchanged (fast tier).
   - New `rules_match.semantic(utterance, context)`: rule summaries across
     enabled apps, focused window, last result -> {rule_id, slots, confidence}.
     Reuses the router chain from `rules_ai._ask_llm` with a 3 s budget; on
     timeout the command routes as before.
   - Confidence < threshold -> "Did you mean ...?" through `dialogue_state`;
     yes adds the phrase to `triggers` (rule learns).
   - "click the first one", "next" bind to `dialogue_state` last result and
     the focused app, not to "open a site".

4. **Know it worked** (outcome)
   - After a run, check `expects`: `browser_cdp._confirm_playing`, page URL,
     window title, ability result.
   - Mismatch -> one sentence on what happened + "change the rule to ...?";
     yes -> `handle_clarification` with the feedback.
   - Corrections observed into `preferences` (same as site defaults today).

5. **Verify**
   - Unit: phrase battery (the 15 cases from 2026-09-15) asserts type,
     condition, gaps; spoken-variant battery asserts semantic tier hits.
   - Live: brave "play music" (client-rendered), notepad "save it" (ctrl s),
     spotify "gym time" (desktop controls), spotify "never open after 11pm"
     (condition), from the phone.

## Order and size

Phase 1 first (one day, unblocks the user's current errors). Phase 2 and 3
can run in parallel (separate files: rules_ai/rules_compiler vs a new
rules_match). Phase 4 after 3. Phase 5 alongside each.

## Open questions (owner)

- Semantic tier on every miss, or only when a command verb is heard? (Cost:
  one short model call per unmatched command.)
- Which app's rules count when nothing is focused: all enabled apps, or the
  app named in the sentence only?
- Should a rule learned trigger be saved silently, or shown in the panel as
  "also works for" for the user to remove?

## Built (2026-09-15)

Owner's decisions: semantic tier runs on every miss; rules from all enabled
apps count, the app in front first; a learned trigger is saved into the rule
and shows as "also works for".

**Phase 1** — rules_steps `_open` records `run["url"]` so a click hits the tab
it opened; `_LOCATE_JS` and `site_probe.click_target_in_dom` match whole
words ("play" no longer hits "New playlist"); `SAFE_KEYS_RE` allows Ctrl/Alt/
Shift letters and F-keys (Ctrl+S, Alt+F4), never Win; slot text keeps
interior words ("hello there"); app-shell pages simulate as "unverified";
`describe_step("open")` says "open the site" unless a pick precedes;
`POST /apps/rules/test` returns `question`, new `POST /apps/rules/test/answer`
feeds yes/no to the waiting run, and the panel shows Yes/No buttons.

**Phase 2** — every phrase reaches `rules_ai.compile_rule` (the keyword
scaffold is only the no-model fallback). The prompt carries the app's known
controls/abilities and existing rules, returns `goal`, `condition`,
`expects`; `_clean_steps_report` returns what it dropped; a desktop click
target not among known controls, an "other" condition, and dropped steps
become `gaps` the proposal reads out ("Heads up: ..."). Well-known site names
resolve (`_KNOWN_SITES` + web_registry). `rules_engine.condition_holds`
evaluates time_after / time_before / time_between; `check` and
`evaluate_app_rules` skip a rule outside its time.

**Phase 3** — new `rules_match.py`: `semantic(utterance)` lists rule
summaries (front app first) with the screen context and asks the router
(5 s, never local Ollama) for {rule, slot, confidence}. In
`tools.try_rule_action`: fast tier first; on a miss, sure (>= 0.75) runs and
learns the phrase as a trigger, unsure (>= 0.4) asks "Did you mean your
... rule?" and a yes runs + learns. A rule outside its time condition says so
instead of running.

**Phase 4** — `expects` is read back after the first two runs ("You should
see: music is playing. If not, say 'that didn't work'"). "That didn't work
[, it should ...]" reworks the last rule that ran (`rules_voice.begin_fix`,
same rule_id, merge commit); a yes saves, anything else is another
correction, no/never mind keeps the old rule.

**Verified** — tests/test_rules_intent.py (22) plus the existing rule suites:
861 pass. Live against the router: the 9-phrase battery now yields a volume
cap, a 23:00 time condition, Ctrl+S, music.youtube.com from "youtube music",
Discord mute as a click, "unsaved changes" reported as a gap. Semantic tier:
10/10 (7 spoken variants matched with slots, 3 non-commands none), 3.6-6.7 s
each.

**Not done / known** — the running server (port 8001) still has the old
code until restarted. Desktop key guesses ("^+{F10}" for AutoSum) cannot be
verified at setup. The block summary wording ("I won't open spotify when
this applies: ...") is still clumsy. Every unmatched command now pays one
router call (~4 s) - the owner's choice; `rules_match._MIN_WORDS` and
`_SKIP_RE` are the only guards.
