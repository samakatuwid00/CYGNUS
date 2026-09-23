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

- [ ] **Bring your own model.** The brain reaches a local 9router that proxies
      the owner's personal Antigravity / Codex / Qoder accounts. Students
      cannot use that, and those providers' terms do not allow reselling the
      access. Cygnus needs a first-class path to: an OpenAI-compatible
      endpoint with the student's own key, or Ollama alone, with no 9router
      installed. 9router stays the owner's fast path.
- [ ] **A machine that is not this one.** Hard-coded assumptions to remove or
      make optional: `C:\Users\deped\...` in `config/opencode.json`, the
      Second Brain vault path, Hermes, Tailscale, the Brave/Chrome profile
      paths, `JARVIS_*` names in `.env`.
- [ ] **Safety defaults for a stranger.** The safety gate is new
      (`cygnus/safety.py`). For a public build: abilities that type into a
      terminal off by default, the app scan opt-in, and one page that says
      plainly what Cygnus can do to the machine and what always asks first.
- [ ] **Install story.** `pyproject.toml`, a licence, a setup guide for a
      clean Windows laptop, and a "first five minutes" walkthrough.
- [ ] **What it does with the microphone and the cloud, in writing.** Speech
      is transcribed locally; turns and their context go to a cloud model. Say
      so, and say what is kept on disk (sessions, memory, audit).

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
