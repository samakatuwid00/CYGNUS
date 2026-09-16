# PANELS_LIVE_PLAN.md — side panels show live state, not a fixed picture

Status: planned, not started (2026-09-13). Owner asked for a plan first.

## What is there today

Screen-size layout is already handled (RESPONSIVE_PLAN.md, commit 94ecb98):
3 columns at 1100px+, a tabbed Panels drawer below that, a docked sheet
on phones. The gap is content: several panels paint once from the HTML
and never change, even though the server already knows the real value.

| Panel / field | Today | Server already has it? |
|---|---|---|
| Voice Feedback "Mode" row (`jarvis_hud_v3.html:1083`) | Hardcoded "Essential" | Yes, `/status.feedback_mode` |
| Voice Feedback chips (`:1086`) | "Essential" active on load; only change on click | Yes, same |
| Voice Feedback timings 4s / 12s / 45s (`:1089-1091`) | Hardcoded text | Partly: `voice_feedback.THINKING_CUE_S`, `THINKING_ESCALATE_S` (config), not in `/status` |
| Top bar "VOICE: KOKORO" / "FB: ESSENTIAL" pills (`:967-968`) | Hardcoded | Yes, `/status.tts`, `/status.feedback_mode` |
| System Status "STT" and "Vault index" (`:991`, `:993`) | Filled only in DEMO; "—" in real use | STT yes (`/status.voice`); vault no |
| Language Model panel (`applyLeft`, `:1533`) | "—" until the first turn's stats arrive | Model name yes (`/status.brain`); meta comes per turn |
| Now Playing (`:3316`) | Polled every 30s; STOP YT / SPOTIFY give no feedback and do not refresh | Yes, `/music/now`, `/music` result |
| Active Tasks, Running Now, Last Turn | Live already | — |

## Plan

1. **One status feed for every panel.** `refreshServerStats()` (`:1571`)
   already fetches `/status`; extend it to also set the feedback chips +
   Mode row, the VOICE / FB pills, STT, and the Language Model name. No new
   polling.
2. **Server: add the missing fields to `/status`** (`jarvis_web.py:2282`):
   `feedback_timings` (cue / escalate seconds from `voice_feedback`), and a
   cheap `vault` readout (index age or "off"). Values that are unknown stay
   "—"; the HUD never pads with plausible numbers (rule at `:1484`).
3. **Voice Feedback reads, then writes.** Timing rows come from
   `feedback_timings`; the third row only shows if the server has a third
   threshold. Clicking a chip updates the pill and Mode row too, and a
   failed POST reverts the chip instead of pretending it switched.
4. **Now Playing reacts to its buttons.** Buttons show a busy state while
   the POST runs, then refresh `/music/now` once. Poll stays at 30s
   (that endpoint drives a Chromium worker, ~2s per call), and also
   refreshes after a play_music / stop_music turn ends.
5. **Language Model fills on page open** from `/status`, not only after a
   turn; per-turn stats keep overwriting it as now.
6. **Push, don't only poll** (optional, step 2 of 2): when the server changes
   feedback mode or music state, send a small `{type: "panel", ...}` over the
   existing `/ws`, so a second window (phone + desktop) stays in sync.
7. **Layout check at the same time.** Playwright at 1920, 1280, 1100, 1024,
   768, 400x860, 860x400: confirm the new values do not overflow the 260px
   columns or the drawer (long model names, long track titles get the
   ellipsis + hover title already used by `.run-name`).

## Constraints

- `jarvis_hud_v3.html` has uncommitted Running Now work (running_apps);
  build on top of it, do not revert it.
- Desktop at 1280px+ must look the same apart from the now-correct values.
- `hud_artifact.html` stays untouched.

## Verify

- Python tests for the new `/status` fields.
- Playwright: switch mode by POST, the chips, Mode row and FB pill all follow
  within one status refresh; STOP YT button shows busy then a new track state.
- Screenshots at the sizes in step 7.
