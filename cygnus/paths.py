"""paths.py - where every file Cygnus reads or writes lives.

Each module used to work out paths from its own location
(os.path.dirname(__file__)), about sixty times, so moving a file quietly
changed where it looked for logs, memory and config. Every path now comes
from here, and moving the code or the data changes only this file
(docs/plans/RESTRUCTURE_PLAN.md).

  ROOT        the repository (the folder holding pytest.ini and desktop/)
  CONFIG_DIR  committed settings: aliases, agents, projects, registries
  WEB_DIR     the pages the server serves
  DATA_DIR    what Cygnus writes: logs, memory, sessions, models, scans.
              CYGNUS_DATA_DIR in the environment puts it elsewhere.
  ENV_FILE    the settings (.env). CYGNUS_ENV_FILE puts it elsewhere.

Installed (the desktop app's installer), the code sits read-only under the
app's resources/ and the desktop app points CYGNUS_DATA_DIR and
CYGNUS_ENV_FILE at the user's own folder (%APPDATA%/Cygnus).
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_root() -> Path:
    """The folder holding cygnus/ and web/: the repository, or the installed
    app's backend folder, which has no pytest.ini or desktop/ beside it."""
    here = Path(__file__).resolve().parent
    for d in (here, *here.parents):
        if (d / "cygnus").is_dir() and (d / "web" / "jarvis_hud_v3.html").is_file():
            return d
    return here


ROOT = _find_root()
CONFIG_DIR = ROOT / "config"
WEB_DIR = ROOT / "web"
STATIC_DIR = WEB_DIR / "static"
EVAL_DIR = ROOT / "eval"
SCRIPTS_DIR = ROOT / "scripts"
DATA_DIR = Path(os.getenv("CYGNUS_DATA_DIR") or ROOT / "data")

LOG_DIR = DATA_DIR / "logs"
MEMORY_DIR = DATA_DIR / "memory"
SESSIONS_DIR = DATA_DIR / "sessions"
MODELS_DIR = DATA_DIR / "models"

# Committed configuration.
APP_ALIASES = CONFIG_DIR / "app_aliases.json"
CLI_AGENTS = CONFIG_DIR / "cli_agents.json"
DELEGATE_REGISTRY = CONFIG_DIR / "delegate_registry.json"
# The names you say (projects, people, apps). Yours in data/, else the
# small example in config/.
INTAKE_CORPUS = DATA_DIR / "intake_corpus.json"
INTAKE_CORPUS_EXAMPLE = CONFIG_DIR / "intake_corpus.json"
# Yours live in data/; config/ ships the example copied on first use.
PROJECTS = DATA_DIR / "projects.json"
PROJECTS_EXAMPLE = CONFIG_DIR / "projects.example.json"
MODELFILE = CONFIG_DIR / "Modelfile.jarvis"
# OpenCode reads opencode.json from the project root itself: it stays there.
OPENCODE_JSON = ROOT / "opencode.json"
ENV_FILE = Path(os.getenv("CYGNUS_ENV_FILE") or ROOT / ".env")

# Pages.
HUD_HTML = WEB_DIR / "jarvis_hud_v3.html"
HUD_ARTIFACT_HTML = WEB_DIR / "hud_artifact.html"
APPS_PANEL_HTML = WEB_DIR / "apps_panel.html"

# Written by Cygnus (scans, profile, caches).
APP_REGISTRY = DATA_DIR / "app_registry.json"
WEB_REGISTRY = DATA_DIR / "web_registry.json"
CAPABILITIES = DATA_DIR / "capabilities.json"
PROFILE = DATA_DIR / "jarvis-profile.md"
MANUS_COOKIES = DATA_DIR / "manus_cookies.json"
WARM_HARNESS_DIR = DATA_DIR / ".warm_harness"
HERMES_JOBS_DIR = DATA_DIR / ".hermes-jobs"
PROJECTS_LOG = DATA_DIR / "projects.log"


def log(name: str) -> str:
    """A file in the log folder, as a string (most callers use os.path)."""
    return str(LOG_DIR / name)


def cwd_for(task=None, jid: str | None = None) -> str:
    """The folder an agent working on `task` runs in: the connected project the
    task names, else the active project, else ROOT (docs/CONNECTOR.md). With
    `jid`, the project is recorded on that job for "what did it change?".

    Agents used to run in ROOT whatever the task was about."""
    try:
        from cygnus.agents import project
        pid, folder = project.project_for(task)
        if jid:
            project.record_job(jid, pid, folder)
        return str(folder)
    except Exception as e:
        print(f"[paths] cwd_for failed ({type(e).__name__}: {e}); using ROOT", flush=True)
        return str(ROOT)
