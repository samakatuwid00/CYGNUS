# Multi-route plan

`multi` is 0% everywhere it is measured: 0/33 on the gold set, 0/7 on live
turns, 0/9 on the stress set. This is what is actually wrong, in the order it
has to be fixed, and the numbers behind each step.

## It is three gaps, not one bug

An earlier brief called this a one-line guard bug at `brain_gemini.py:2464`.
That was wrong. Three separate things are missing and the predicate is the only
one of them that is nearly solved.

**1. No predicate that separates a chain from a delegated goal.**
`_is_multistep_goal` fires on 64 gold rows: 32 are labelled `task`, 14 `multi`,
18 something else. Routing its hits to `multi` gains 14 rows and loses 50. It
detects "multi-step goal", which in these labels usually means *hand it to an
agent* - a different thing from *do these two local actions*.

**2. No execution path.** `run_autonomous` launches a supervised Hermes job.
There is no local chained-command executor. "close notepad and spotify" today
is either classified by its first clause as `close_app` and half-executed, or
handed to a background agent to close two apps.

**3. No instrumentation.** `routing_eval.live_route` maps a turn to exactly one
action type, and `keyword_route` has no branch that returns `"multi"` at all.
A turn that does two things is recorded as one. `multi` cannot be scored by
either path even if the routing were perfect.

Gap 3 is why `eval/gate_routing.py`'s two `multi` checks cannot pass today, and
why an autonomous run that was told to make them pass edited the predictor
instead. A requirement that looks satisfiable and is not invites exactly that.

## The predicate: measured, not implemented

`_is_chained_command` is the right idea and already exists - clauses each led by
a device or media verb, counted over the outer request so a quoted chain is not
the owner's chain. Two refinements, each measured against the 608-row gold set:

| predicate | fires | precision | recall | F1 |
|---|---|---|---|---|
| `_is_chained_command` today | 25 | 56% | 42% | 48% |
| + verb elision | 34 | 56% | 58% | 57% |
| + a target must be named | 30 | 63% | 58% | 60% |
| + delegation veto | 25 | **72%** | **55%** | **62%** |

**Verb elision.** The clause splitter records a verb only when a clause starts
with one, so "close notepad and Spotify" splits to `["close notepad",
"Spotify"]`, yields one verb, and fails the two-distinct-verbs test. Most of the
19 missed `multi` rows are this shape: one verb, several targets ("close
spotify, notepad, and calculator", "close powerpoint and cmd"). Carry the verb
across a clause that has none, but only when the bare clause resolves to a real
app or site - otherwise any trailing phrase reads as an action.

**A target must be named.** ASR stutter produces empty targets: "Open, open
code, Jarvis." gives `('open',''), ('open','code')`; "Open, read, browse."
gives three verbs and no objects. Requiring a non-empty target drops five false
positives.

**Delegation veto.** A chain containing a retrieval or reporting clause is
delegated work, not a device chain: "open chatgpt and search in history irimsv
project report and tell me the latest reply" is labelled `task`. Veto the whole
chain when any clause verb is one of tell me / read / summarize / check /
verify / scan.

If chains routed to `multi` on this predicate, gold accuracy goes **54.9% ->
57.6%** (+18 rows correct, -2 rows lost) and `multi` recall goes **0/33 ->
18/33**. It is a net gain, unlike `_is_multistep_goal`, which would lose 50.

## Why the predicate is not landed yet

Wiring it into `_is_multistep_goal` without gap 2 makes things worse, not
better: more chains would be detected and every one of them delegated to a
Hermes job. "Close notepad and Spotify" becoming a background agent task is a
regression over today's behaviour, where at least one of the two apps closes
immediately.

Order matters. Local executor first, then the predicate routes to it, then the
instrumentation records it, then the eval can score it.

## Suggested order

1. **Local chained executor.** Take the `(verb, target)` list the predicate
   already produces and run each action through the existing ability handlers
   in sequence, stopping on the first failure and saying which step failed.
   Confirm-gated as a whole if any step is risky.
2. **Land the predicate** and route its hits to that executor instead of
   `run_autonomous`. Expected: gold 54.9% -> 57.6%, multi recall 0 -> 18/33.
3. **Instrument it.** A chained turn needs to record its action list, so
   `live_route` can return `multi` and the live numbers stop reading 0/7 for a
   reason that has nothing to do with routing.
4. **Mirror it in `keyword_route`,** gating on exactly what production gates on.
   The replay exists to predict production; a branch tuned for score rather than
   fidelity is worse than no branch.
5. Only then do the two `multi` checks in `eval/gate_routing.py` mean anything.

## Open question for review

The gold `multi` labels are `claude-draft` and unreviewed, like the rest of the
608. Several look arguable - "Open Spotify and search for this online option."
is labelled `multi`, and "Can you open Spotify and play some music?" too, when
opening Spotify and playing are arguably one intent with one handler. The 72%
precision figure above may be measuring against labels that are themselves
uncertain. Worth reviewing the 33 `multi` rows by hand before building an
executor to serve them.
