# Opening Cygnus to students

Status: audit done 2026-09-23. Not ready to publish yet; the blockers below
are in the order they have to be cleared.

## 1. What the history holds (audited 2026-09-23)

Scanned: every distinct blob reachable from `master` (1392 of them) against
patterns for OpenAI/Anthropic/Google/GitHub/Slack/Stripe/AWS keys, private-key
blocks, `api_key=`/`secret=`/`password=` assignments and cookie jars.

- **No secrets.** The only hits are variable names (`api_key=CEREBRAS_API_KEY`).
  `.env` was never committed; `.env.example` carries `dummy` values.
- **Personal data is in the history**, added before it was gitignored:
  - `web_registry.json` (added 31b1c20, 2026-08-24; deleted aa2bece,
    2026-09-01): the owner's browsing history - sites, hosts, visit counts and
    top URLs, including private ones (`irimsv.net`, 1001 visits).
  - `_utts.json`, `_eval_set.json`: the owner's own spoken utterances, with
    personal asides and business names.
  - `logs/audit.jsonl`: test rows only (`unit_test_tool`), harmless.
- `resources/views/...`, `storage/logs/...` (an unrelated Laravel project) are
  reachable only from the local `archive/vk-c9a3-hi` branch, never pushed.

**Decision needed.** Two ways to publish:

1. **A fresh public repository** (recommended): one "initial public release"
   commit of the current tree. This private repo keeps the full history. No
   chance of leaking anything from it; the public repo has no history.
2. **A scrubbed export** with `git filter-repo`, dropping those paths from a
   clone, then pushing that clone to the public repo. Keeps history, costs more
   work, and every future contributor sees rewritten hashes.

Either way: never push `--all` or `--tags` from this repo, and never push the
`archive/vk-c9a3-hi` branch.

## 2. Blockers before anyone else can run it

- [x] **Bring your own model** (done 2026-09-23). The brain reaches a local 9router that proxies
      the owner's personal Antigravity / Codex / Qoder accounts. Students
      cannot use that, and those providers' terms do not allow reselling the
      access. Cygnus needs a first-class path to: an OpenAI-compatible
      endpoint with the student's own key, or Ollama alone, with no 9router
      installed. 9router stays the owner's fast path.
- [x] **A machine that is not this one** (done 2026-09-23). The vault, the
      reports folder, the Whisper word list, the Groq and Cerebras key paths
      and the project list are settings or examples now; the owner's own
      projects and spoken names moved to `data/`. Verified by starting Cygnus
      with a fresh data folder, no vault and no microphone: it came up and
      answered. Left: the `JARVIS_*` prefix on every setting, and Hermes and
      Tailscale, which are optional already but not documented as such.
- [x] **Safety defaults for a stranger** (done 2026-09-23): abilities that
      type into a terminal arrive switched off and set to ask; reading the
      browser history is opt-in (it ran unprompted on a fresh install, found
      by starting Cygnus as a stranger would); docs/WHAT_IT_CAN_DO.md says
      what Cygnus can do, what it always asks about, what it refuses, and
      what leaves the machine.
- [x] **Install story**: `pyproject.toml` and docs/SETUP.md (a clean
      Windows laptop, a first five minutes, and what to do when it breaks) are
      done, and an installer (README, "Building the installer"). **Licence**
      (2026-09-30, the owner's choice): PolyForm Noncommercial 1.0.0, free for
      students and schools, a paid licence for commercial use (LICENSE.md,
      COMMERCIAL.md, THIRD_PARTY_NOTICES.md).
- [x] **What it does with the microphone and the cloud, in writing**
      (docs/PRIVACY.md, 2026-09-23). Written by auditing the code, with a
      file:line for every claim. It found three things the docs had wrong or
      silent about:
      - the default voice (edge-tts) uploads the text of **every reply** to
        Microsoft, so "nothing leaves with a local model" was false;
        JARVIS_LOCAL_ONLY now refuses it instead of falling back to it.
      - the HUD loaded `lucide@latest` from unpkg, unpinned and without SRI,
        into the page that holds the assistant's socket. Vendored.
      - the wake listener writes what it heard to data/logs/wake_debug.log;
        harmless with the keyword spotter, a transcript of the room without it.
- [ ] **Retention.** Nothing is ever deleted: sessions, audit, shadow and the
      backend log (7.2 MB, verbatim speech) grow forever, and there is no
      "forget everything" command. PRIVACY.md documents the gap and how to
      delete by hand; it should be a setting and a command.

## 3. What "production ready" means, in numbers

Targets to hold before publishing (measured, not felt):

| What | Measure | Target |
|---|---|---|
| Routing | Labelled utterances, deterministic, no model | 95%+ on routes that act |
| Safety | Destructive and ambiguous requests that act without asking | 0 of 500 |
| Hallucination | Reply's claims vs the audit log; questions with a checkable answer | unsupported claims < 2% |
| Speed | Percentiles per route; time to first audio | instant < 300 ms, agent p95 < 20 s |
| Fallback | 9router down, quota 429, Ollama off, network off, mic busy | answers or says why < 15 s, never claims success |
| Concurrency | Phone + desktop + voice interleaved | no cross-talk, confirms stay per client |
| Time to task | 20 real tasks: the owner with a stopwatch vs Cygnus | wins on chains, never worse than 2x on singles |
| UI/UX | Heuristic and accessibility review, HUD and panel, phone | no unlabelled controls, progress shown < 1 s |
| Soak | 24 h of normal use | no crash, no runaway job, flat memory |

The harness for the first five rows can be automated. The labelled utterances
and the stopwatch baseline need the owner: invented sentences do not measure
this, and the time-saved number is what a public README has to show.

**Concurrency (2026-09-23).** `scripts/bench/concurrency.py`: two clients on
one server, five scenarios. It found the confirm latches were global - a "yes"
on the desktop released a delete the phone had been asked about ("confirmed -
launching Hermes" in the job's own progress). Every latch now records which
client was asked, only that client can answer, and a stray "yes" is told where
the question is waiting. Cross-talk, context isolation and ten interleaved
turns were already clean. Voice and typed at the same moment is still
untested: the bench drives two websocket clients, not a microphone.

**Routing (2026-09-23).** `scripts/bench/routing.py` mines the real utterances
out of `data/sessions/` (2018 turns, 1082 distinct, 749 groups), groups the
phrasings that route the same way, and puts the 150 that matter most up for
review - doubtful first, then the routes that act. `accept` folds them into
`data/routing_gold.jsonl`, `score` measures. Auto-accepted rows are marked
`proposed` and counted separately, because the classifier agreeing with itself
is not a measurement: **the number only becomes real once the owner has read
the review file.** Its first run already found a live bug - Whisper's commas on
staccato speech ("Open, Telegram.") sent real commands to the model as
chit-chat; fixed, with tests.
