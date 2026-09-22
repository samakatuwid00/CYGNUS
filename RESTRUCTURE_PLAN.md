# Restructure plan: from flat root to a `cygnus/` package

Status: proposed, 2026-09-22. Cutover from jarvis-demo in progress; nothing restructured yet.

## Where it stands

- 356 tracked files, about 100 Python modules loose in the repo root, next to
  17 `*_PLAN.md` notes, HUD HTML, JSON config, PowerShell helpers, diag
  scripts, logs and backups.
- Three files hold most of the code: `tools.py` (6235 lines, 161 functions,
  imported by 53 modules), `brain_gemini.py` (3784 lines), `jarvis_web.py`
  (2760 lines).
- Almost every import is a lazy import inside a function (`from tools import
  x`), often to dodge cycles. `tools` and `brain_gemini` import each other.
- Paths are worked out from each file's own location: `tools.py` does
  `os.path.dirname(__file__)` 24 times, `jarvis_web.py` 6 times, about 20 other
  modules once or twice. Moving a file today silently changes where it looks
  for `logs/`, `memory/`, `delegate_registry.json` and the HUD HTML.
- Tests find modules because pytest runs from the root. `tests/conftest.py`
  monkeypatches module globals (`rules_sim.PROBE`, `tools._PENDING_RULE_SLOT`),
  so a move must keep one module object per name.
- `__main__.py` imports `jarvis.main`, which does not exist. It is broken now.
- `loop.toml` freezes `routing_eval.py`, `stress_eval.py` and
  `eval/gate_routing.py` (by path and hash). `desktop/main.js` starts
  `jarvis_web.py` by name.

## Target layout

```
CYGNUS/
  cygnus/                    all runtime code, one package
    __main__.py              python -m cygnus  (starts the server)
    paths.py                 ROOT, CONFIG_DIR, DATA_DIR, LOG_DIR, MEMORY_DIR, WEB_DIR
    config.py
    server/                  app.py (was jarvis_web.py); routes split out later
    brain/                   brain_gemini, intent_router, semantic_route,
                             router_health, command_chain, agent_loop,
                             action_gate, consent, dialogue_state, plain_reply,
                             intake, context_assembler, conversation_window,
                             transcript_pick
    tools/                   tools.py first as __init__.py, split later
    apps/                    app_abilities, app_commands, running_apps,
                             machine_capabilities, curate, desktop_driver,
                             web_registry
    rules/                   rules_engine, rules_compiler, rules_ai,
                             rules_match, rules_sim, rules_steps, rules_voice
    browser/                 browser_agent, browser_cdp, site_probe, guard,
                             mcp_browser_server, manus_agent
    voice/                   voice_engine, wake_engine, wake_kws,
                             voice_feedback, voice_ducking
    memory/                  semantic_memory, memory_hygiene, session_store,
                             session_index, preferences
    jobs/                    jobs, autonomous, warm_harness, audit
    agents/                  music_agent, music_rules, report_agent,
                             project_agent
  web/                       hud.html, apps_panel.html, hud_artifact.html, static/
  desktop/                   Electron app (unchanged apart from the start command)
  config/                    committed config: app_aliases, delegate_registry,
                             cli_agents, projects, ok_models, opencode.json,
                             Modelfile.jarvis
  data/                      gitignored runtime state: memory/, sessions/, logs/,
                             models/, app_registry.json, web_registry.json,
                             capabilities.json, jarvis-profile.md
  eval/                      routing and stress evals plus their data
  tests/                     all tests, including the stray root *_test.py
  scripts/{dev,diag,bench,probe}/
  docs/{plans,reviews,mockups}/
```

Rules that go with the layout:

1. Code lives under `cygnus/`. The root holds only the README, `requirements.txt`,
   `pyproject.toml`, `pytest.ini`, `.env.example`, `loop.toml` and folders.
2. No module works out a path from its own `__file__`. Every path comes from
   `cygnus/paths.py`.
3. Committed config goes in `config/`. Anything the app writes goes in `data/`
   (gitignored).
4. Imports are absolute: `from cygnus.rules import engine`.
5. One test file per module area in `tests/`. No `*_test.py` beside the code.

## Phases

After every phase, all of these must pass before the next phase starts:

- `python -m pytest -q`: 993 passed (measured 2026-09-22; `loop.toml`'s
  comment still says 860).
- `python eval/gate_routing.py`
- `python -m compileall -q cygnus tests`
- The server starts and `GET /`, `/apps.html` and `/hud.html` return 200.
- `npm run dev` in `desktop/` attaches to the server or starts it.

Each phase is one commit (or a small series), so it can be reverted on its own.

### Before phase 0: make CYGNUS the only working copy, then archive jarvis-demo

CYGNUS was copied from `Documents\jarvis-demo`. On 2026-09-22 the code in both
matched byte for byte, and the tests (993 passed) and `eval/gate_routing.py`
passed in both. jarvis-demo was still the copy in daily use, though: the
desktop shortcut started it, and its memory, sessions and `.env` were newer.

Done on 2026-09-22:

- [x] **Git history.** jarvis-demo's 149 commits are now CYGNUS's history. The
  baseline commit sits on top of jarvis-demo's `d38043d`, and the four CYGNUS
  commits follow it. The tree is unchanged (checked with `git diff`). The old
  history is kept as branch `backup/pre-history-merge`. jarvis-demo's
  branches were fetched as `jarvis-demo/*`, its remote-tracking refs as
  `jarvis-demo-origin/*`, and tag `agentloop-preport` came across.
  `origin` = `samakatuwid00/CYGNUS.git`; pushing `master` to both `main` and
  `master` would be a fast-forward. **Not pushed yet.**
- [x] **venv.** `.venv` was rebuilt in place from the same `pip freeze` (102
  packages, identical). The copied one pointed its `pip.exe`, `pytest.exe`
  and activate scripts at `jarvis-demo\.venv`. The old copy is
  `.venv.jarvis-copy/`; delete it after a week of normal use.
- [x] **Desktop shortcut.** `Desktop\Cygnus.lnk` now starts
  `CYGNUS\desktop`. The old one is saved at
  `.recovery/cutover-2026-09-22/Cygnus.lnk.jarvis-demo`.
- [x] **`opencode.json`.** The model now matches jarvis-demo
  (`ag/claude-sonnet-4-6`), and the app launcher reads CYGNUS's
  `app_registry.json`.
- [ ] **Runtime data.** Run `bash scripts/cutover_sync.sh`, then add
  `--apply` to it. It copies jarvis-demo's `.env`, `memory/`, `sessions/`,
  `logs/`, `app_registry.json` and `web_registry.json` (32 files) into CYGNUS,
  backing up each CYGNUS file it replaces to
  `.recovery/cutover-2026-09-22/cygnus-before/` first.

Archiving jarvis-demo (after about a week of running from CYGNUS without
problems, so around 2026-09-29):

1. Check that nothing still points at jarvis-demo: running processes,
   shortcuts, scheduled tasks, `~/.hermes`, `~/.claude`. On 2026-09-22 the
   only outside reference was a note in `~/.hermes/plans`.
   `My Documents\jarvis-demo` is the same folder seen through the
   `My Documents` link, not a second copy.
2. Decide what to do with the loose ends in jarvis-demo:
   - Branch `vk/c9a3-hi` has a vibe-kanban worktree in
     `%TEMP%\vibe-kanban\worktrees\c9a3-hi`. Its commits are already in
     CYGNUS as `jarvis-demo/vk/c9a3-hi`. Keep it as a local branch, or drop it.
   - The `irms` remote points at the unrelated iRIMS repo. Do not carry it over.
   - `.preport-backup/` holds pre-port copies of five files. It is already
     covered by the history, so drop it.
3. Push CYGNUS: `git push origin master:main master:master`. Both are
   fast-forwards; check again first with
   `git fetch origin && git merge-base --is-ancestor origin/main master`.
4. Rename `Documents\jarvis-demo` to `jarvis-demo.ARCHIVED`. For one week,
   anything still using the old path fails loudly instead of quietly using
   stale code.
5. Zip it without the parts that can be rebuilt (`.venv/`,
   `desktop/node_modules/`, `models/`, `__pycache__/`) into
   `Documents\Archive\jarvis-demo-<date>.zip`. Check that the zip lists
   `memory/semantic.db` and `sessions/index.db`, and that their hashes match.
6. Delete `jarvis-demo.ARCHIVED`, `.venv.jarvis-copy/`, and the
   `jarvis-demo/*` and `jarvis-demo-origin/*` refs in CYGNUS.
7. Phase 0 removes the dozen dev scripts that still use the old path
   (`diag_*`, `analyze.py`, `check_files.py`, `debug_stdin.py`,
   `examine_code.py`, `_hud_edit.py`, `benchmark_turbovec_vs_substring.py`).

### Phase 0: clear the clutter (no code changes)

- Delete throwaway files: `_probe.py`, `.tmp-getvol.py`, `.tmp-setvol40.py`,
  `verify_out.txt`, `Ollama`, `.patchtmp/`, `jarvis_hud_v3.html.bak-r3`,
  `opencode.json.bak-9router`.
- Delete untracked junk on disk: `chrome_dbg.log` (99 MB), `*.log` in the root,
  the file named `C:UsersdepedAppDataLocalTempjarvis-server.log`, and the
  `*.bak` files.
- `git mv` the 17 `*_PLAN.md` files and the `JARVIS-*` reviews into
  `docs/plans/` and `docs/reviews/`. Move the mockups and `jarvis_visual.html`
  into `docs/mockups/`.
- Gitignore `playwright_shots/` (screenshots are output, not source) and
  untrack it.
- Sort the one-off scripts (`diag_*`, `examine*`, `analyze*`, `check_files`,
  `verify*`, `ws_*`, `turbovec_*`, `benchmark_*`, `debug_stdin`, `test_input`,
  `test_loop`, `capture_page`, `_hud_edit`) into `scripts/diag|bench|probe/`,
  or delete them if they are dead. Most are from August and nothing imports them.

### Phase 1: one source for paths (files stay where they are)

This phase makes the later moves safe.

- Add `paths.py`, with `ROOT` found once: the folder that holds `.env.example`.
- Replace every `os.path.dirname(os.path.abspath(__file__))` and
  `Path(__file__).parent` in runtime code with a name from `paths`. That is
  about 60 places, mostly in `tools.py` and `jarvis_web.py`.
- Remove the `sys.path.insert` calls in `tools.py` (lines 3011, 4893, 5492,
  5505).
- Nothing moves, so behaviour must not change. The gates prove it.

### Phase 2: create the package and move the files

- Add `pyproject.toml` (package `cygnus`), then `pip install -e .` into
  `.venv`, so tests and scripts import `cygnus` without path tricks.
- `git mv` each module into its subpackage (history is kept).
- Leave a small alias module at each old root name so old imports and
  `__import__("jobs")` strings keep working during the change:
  ```python
  # tools.py (temporary alias)
  import sys, cygnus.tools as _m
  sys.modules[__name__] = _m
  ```
  It must be a `sys.modules` alias, not `from cygnus.tools import *`. A star
  import makes a second module object, so `monkeypatch.setattr(tools, ...)` in
  the tests would patch the copy and the real code would never see it.
- Move one subpackage at a time, from the leaves inward, and run the gates
  after each: rules, voice, memory, jobs, browser, apps, agents, brain, tools,
  server.
- Move the HTML into `web/`, and the committed JSON into `config/`
  (by updating `paths`).
- Add `cygnus/__main__.py`. Point `desktop/main.js` at `-m cygnus` instead of
  `jarvis_web.py`, and do the same in `watch_and_restart.py` and
  `make_shortcut.ps1`. Delete the broken root `__main__.py`.

### Phase 3: rewrite the imports and remove the aliases

- Rewrite imports with a script that uses the Python parser, not regex:
  `import tools` becomes `from cygnus import tools`, and
  `from rules_engine import X` becomes `from cygnus.rules.engine import X`.
  Also rewrite the `__import__("…")` and `patch("audit.…")` strings.
- Delete the root aliases. A grep for any old bare module name must come back
  empty.
- Move the root `*_test.py` files into `tests/`.

### Phase 4: runtime data into `data/`

- `paths.DATA_DIR` defaults to `ROOT/data` and can be changed with
  `CYGNUS_DATA_DIR`.
- A one-time `scripts/dev/migrate_data.py` moves `memory/`, `sessions/`,
  `logs/`, `models/` and the generated JSON into it. It stops if the target
  already exists, and never overwrites.
- Tidy `.gitignore` down to one rule: `data/`.

### Phase 5: split the three large files (after the others)

Split along the section markers already in the code, re-exporting from the
package `__init__` so callers do not change:

- `tools/`: vault.py, search.py, files.py, sites.py, recall.py, rules_slots.py,
  delegation.py, profile.py, registry.py (it already has `# ---` section
  headers).
- `brain/`: chain.py (the model chain and fallback), state.py, routing.py.
- `server/`: app.py (app, lifespan), ws.py (turns), routes/apps.py,
  routes/running.py, routes/rules.py.

Split one file per commit, with the gates each time. Remove the `tools` and
`brain_gemini` import cycle while doing it: shared state goes in a small
module that both import.

## Risks

| Risk | What guards against it |
|---|---|
| A moved file looks for data in the wrong place | Phase 1 comes first; nothing moves while any path is still worked out from `__file__` |
| Monkeypatches stop reaching the real module | `sys.modules` aliases, never star imports |
| Frozen eval files (`loop.toml`) | Leave `routing_eval.py` and `stress_eval.py` where they are until phase 3, then move them to `eval/` and update `loop.toml` `frozen` and the hashes in the same commit. This needs sign-off, because it is exactly the kind of edit the freeze exists to catch |
| The desktop app starts the old entry point | The alias keeps `jarvis_web.py` working until `main.js` is changed in the same commit |
| The user's real memory and sessions are lost | Phase 4 only moves, never overwrites, and has a dry-run flag. Back up `memory/` and `sessions/` first |
| Outside references (Second Brain notes, `.hermes/plans`, `.claude/settings.local.json` `Edit(jarvis_hud_v3.html)`) | Update the settings file in phase 2. Notes may keep old names; they are history |

## Decisions needed

1. Identifier names: keep `JARVIS_*` environment variables (recommended: they
   are in `.env` on every machine) but name the code `cygnus` and the server
   `cygnus/server/app.py`.
2. The one-off scripts from phase 0: delete them, or keep them in `scripts/`?
3. Move the frozen eval files in phase 3 (with the `loop.toml` update), or
   leave them in the root?
4. Runtime data: move it to `data/` (phase 4), or leave it where it is and
   only point `paths` at it?
