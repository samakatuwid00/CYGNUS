# The Project Connector

Cygnus hands coding work to agents (Claude Code, Hermes, OpenCode, Codex,
Gemini, pi). Until now every one of them ran in Cygnus's own folder, whatever
the task was about. The connector lets you name the folders you work in, pick
the one that is active, and have agents run there - with a yes from you
before the first change an agent makes in a new project.

It has four parts:

| Part | Where |
|------|-------|
| The registry: which folders are projects, which is active | `data/projects.json`, `cygnus/agents/project.py` |
| Routing: agents start in the project's folder | `paths.cwd_for()` and its callers |
| An MCP server agents can use to look at a project | `cygnus/mcp/` |
| The panel, the header switcher, voice | `server/projects_routes.py`, `web/apps_panel.html`, `web/jarvis_hud_v3.html`, `brain/core.py` |

---

## The registry

`data/projects.json` (or `JARVIS_PROJECTS_FILE`) holds one entry per project.
The connector added three fields; entries without them still work and read as
the defaults.

```json
{
  "sticky-brain": {
    "path": "C:/Users/you/Documents/sticky-brain",
    "dev_cmd": "npm run dev",
    "url": "",
    "active": false,
    "agent": "claude-code",
    "allow_writes": false,
    "test_cmd": "npm test"
  }
}
```

| Field | Meaning |
|-------|---------|
| `active` | The project agents work in now. At most one is true. None true means Cygnus's own folder (the built-in `cygnus` project, never written to the file). |
| `agent` | The coding agent this project prefers. Default `claude-code`. Recorded and shown; routing by it is not wired yet. |
| `allow_writes` | An agent may change files here without asking. False until you say yes the first time (below). |
| `test_cmd` | Optional. What `run_tests` runs. Without it the command is read from the project (`npm test`, `pytest`, `php artisan test`, `cargo test`, `go test ./...`). |

`path`, `dev_cmd`, `url`, `vault_secret` and `note` are unchanged
(`launch_project` still uses them).

Connecting refuses a relative path, a folder that does not exist, a drive
root and your home folder: those would let agents work across everything.
Connecting Cygnus's own folder is a no-op - it is always connected.

## Where an agent runs

`paths.cwd_for(task, jid=None)` decides, in this order:

1. the connected project the task names outright, as a whole word
   ("fix the header in **sticky brain**" - hyphens may be spoken as spaces);
2. else the active project;
3. else Cygnus's folder (`paths.ROOT`).

A project whose folder has gone missing falls back to Cygnus's folder rather
than failing the spawn. With `jid`, the job is tagged with the project id and
the commit the folder was at (`project_head`); that is what "what did it
change?" diffs from.

Every place that used to hard-code `cwd=paths.ROOT` for an agent now calls it:
Hermes (`tools.delegate_to_hermes`, through `warm_harness.warm_send(cwd=)`),
the CLI agents (`_run_cli_agent`, `_start_cli_job`), OpenCode
(`_run_specialist`, `_run_specialist_bg`), pi (`_run_pi_specialist[_bg]`) and
autonomous goals (`jobs/autonomous.start_goal`). The look-only check (did
files change during a job that should only have looked?) watches the
project's folder, not Cygnus's.

## The first write

`safety.assess_project_write(task)` runs before a task goes to a coding agent
(`delegate`, `delegate_task`, `delegate_to_hermes[_grounded]`,
`run_autonomous`, and the agent loop's `delegate`). When the task would run in
a connected project whose `allow_writes` is false, and the task is not
look-only, Cygnus asks:

> This would be the first time an agent changes files in sticky-brain
> (C:/Users/you/Documents/sticky-brain). Allow it, sir?

A yes in a later turn sets `allow_writes: true` in `projects.json` and then
runs the task. A no runs nothing and remembers nothing. Look-only tasks
("what does the header do?") and Cygnus's own folder never ask.

The yes is per project and permanent until you set `allow_writes` back to
false in the file.

## Jobs record their project

Each job in the jobs registry carries `project` and `project_head`. The
Running panel shows a project tag on a task card, and a "Project" line in its
details, whenever the project is not Cygnus. `jobs.registry.for_project(id)`
lists the jobs that ran in a project.

---

## The MCP server

```
python -m cygnus.mcp.project_connector
```

A stdio MCP server (JSON-RPC 2.0, one message per line; standard library
only, no `mcp` package needed). To give Claude Code the tools, add to a
project's `.mcp.json` (adjust the paths):

```json
{
  "mcpServers": {
    "cygnus-projects": {
      "command": "C:/Users/you/Documents/CYGNUS/.venv/Scripts/python.exe",
      "args": ["-m", "cygnus.mcp.project_connector"],
      "env": { "PYTHONPATH": "C:/Users/you/Documents/CYGNUS" }
    }
  }
}
```

or `claude mcp add cygnus-projects -e PYTHONPATH=C:/Users/you/Documents/CYGNUS -- C:/Users/you/Documents/CYGNUS/.venv/Scripts/python.exe -m cygnus.mcp.project_connector`.

### Tools

`project` is a project id (or its spoken name) and defaults to the active
project.

| Tool | Does | Arguments |
|------|------|-----------|
| `list_projects` | Every project: id, folder, exists, git, active, agent, writes allowed | - |
| `connect_project` | Connect a folder. Never allows writes. | `path`, `name?`, `dev_cmd?`, `url?`, `active?` |
| `read_file` | One text file, screened (below) | `project?`, `path`, `max_bytes?` (64 KB default, 400 KB max) |
| `get_recent_changes` | Recent commits with their files, uncommitted files, and the Cygnus jobs that ran there with what changed since each started | `project?`, `limit?` |
| `get_git_status` | Branch, commit, uncommitted files, last commits | `project?` |
| `get_git_diff` | Uncommitted, staged, or since a commit; optionally one path | `project?`, `path?`, `since?`, `staged?`, `max_chars?` |
| `list_files` | Files, newest first; `summary` adds the index | `project?`, `path?`, `pattern?`, `limit?`, `summary?` |
| `run_tests` | Runs the tests - only after a yes (below) | `project?`, `command?`, `confirm_token?`, `timeout?` |
| `analyze_code` | Static: lines by language, largest files, TODOs, test files; for a file, its definitions and imports | `project?`, `path?` |

Behind them:

- `scanner.py` - path guards, blocked files, content screening, the file
  listing (skips `node_modules`, `.venv`, `dist`, `build`, `.git`, `vendor`
  and the like) and a per-project index cached for a minute: file counts by
  extension, top-level entries, the stack read from marker files.
- `gitinfo.py` - read-only git with timeouts; nothing commits, checks out or
  resets.
- `analyzer.py` - Python through `ast`, JS/TS/PHP/Go/Rust through patterns.
  Nothing is imported or run.

### Guardrails

These are enforced in the server, not left to the client.

- **Reads stay inside the project folder.** Every path is resolved (`..`,
  absolute paths and links included) and refused unless it is under the
  project's folder.
- **Secrets are never read, listed or diffed.** `.env`, `.env.*`, `*.env`,
  private keys (`*.pem`, `*.key`, `id_rsa*`, `*.p12`, ...), `.npmrc`,
  `.pypirc`, `.netrc`, `.git-credentials`, credential and secrets files, and
  anything under `.ssh`, `.aws`, `.gnupg`, `.git` and the like. Templates
  (`.env.example`, `.env.sample`, `.env.template`) are allowed. Diffs exclude
  the same files through git pathspecs; `git status` hides them.
- **Content is screened.** File text, diffs and test output come back framed
  as `[UNTRUSTED CONTENT from ...]`. Lines that read as orders to an AI
  ("ignore previous instructions", role tags, "do not tell the user",
  `curl ... | sh`) are named in a warning above the text. The text itself is
  not altered - it may be code you want fixed.
- **Tests run only after a yes.** The first `run_tests` call runs nothing: it
  returns the command and a `confirm_token` (five minutes, single use). The
  client asks you, and calls again with the token only if you agree. Output
  is cut to the last 8 KB; a run is killed (with its child processes) at the
  timeout.
- **Nothing writes.** No tool edits a file, commits, or changes
  `allow_writes`. `connect_project` can add a folder (writes off), and can
  make it active.
- Every connect, refused read, test request and test run is appended to
  `data/logs/project_connector.log`.

---

## HTTP

`cygnus/server/projects_routes.py`. Writes take JSON from Cygnus's own pages
only (the same host/origin/content-type guard as `/apps`).

| Endpoint | Body | Returns |
|----------|------|---------|
| `GET /projects` | - | `{ok, projects, active}` (Cygnus first) |
| `POST /projects/connect` | `{path, name?, dev_cmd?, url?, agent?, active?}` | `{ok, project}`; 400 with `error` |
| `POST /projects/disconnect` | `{id}` | `{ok, id}`; the folder is untouched |
| `POST /projects/active` | `{id}` (`cygnus` = Cygnus's folder) | `{ok, project, warning?}` |
| `POST /projects/start` | `{id}` | `{ok, result}` (`launch_project`: dev server, browser, login) |
| `GET /projects/{id}/changes` | - | `{ok, git, branch, head, status, recent, jobs, diff, diff_truncated, diff_base}` |

Connect and disconnect are refused from the phone (proxied requests): which
folders agents may touch is decided at the computer. The changes diff runs
from the commit the newest job started at, or is the uncommitted work when no
job ran there.

## The UI

- **Header**: `Project: <id> ▾`, shown once an outside project is connected.
  A listbox to switch; a missing folder is shown and cannot be picked.
- **Apps panel → Projects tab**: a card per project (active, writes allowed,
  git, folder missing), Set active, Start, Changes (changed files, recent
  commits, agent jobs, diff), Disconnect, and a form to connect a folder.
  Everything from the server is inserted as text, never HTML.
- **Running panel**: a project tag on each task that ran outside Cygnus.

## Voice

Instant and local (`agents/project.voice`, routed from `brain/core.py`):

| Say | Does |
|-----|------|
| "work on sticky brain", "switch to my-app", "let's work on the lrmis-main project" | Makes it active. Only an exact project name counts, so "work on the report" is not a switch. |
| "back to your own code", "switch back to Cygnus" | Cygnus's folder again |
| "connect this folder" | Connects the folder in the front-most File Explorer window |
| "connect C:\code\my app", "... and work on it" | Connects that path (and makes it active) |
| "which project are we on?" | Says the active project |

---

## Known limits

- `agent` is stored and shown but does not pick the agent yet.
- `allow_writes` can be turned back off only in `projects.json`.
- "connect this folder" reads File Explorer only, not an editor's open folder.
- The MCP server is not attached to the agents Cygnus spawns automatically;
  add it to a project's `.mcp.json` (above) to give them the tools.
