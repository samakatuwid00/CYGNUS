"""Project launcher for JARVIS - starts a dev server and opens it in Brave.

Same worker shape as music_agent.BraveMusicWorker and for the same reason:
Playwright objects are thread-affine and its sync API refuses to run inside an
asyncio loop, while jarvis_web calls brain.think() straight from the WebSocket
handler. One dedicated thread owns Brave for the life of the process and every
call is marshalled onto it through a queue.

Deliberately a SEPARATE worker and profile from music_agent's and from
browser_agent's: music must not lose its tab because a project was launched, and
the ChatGPT session belongs to Chrome. Each owns its own profile directory so
none of them fight over a profile lock.

Which projects exist is read from projects.json beside this file; a template is
written on first use. Credentials, when a project has them, come from the vault
through tools.get_credentials and are strictly read - nothing here writes to the
vault, and the password is never logged or returned.

The same file is the Project Connector's registry (docs/CONNECTOR.md). Each
entry may also carry:
  active        the project agents work in now (at most one; none means
                Cygnus's own folder, the built-in "cygnus" project)
  agent         the coding agent the project prefers (default claude-code)
  allow_writes  an agent may change files there; false until the owner says
                yes the first time a job would (safety.assess_project_write)
Older entries without them read as inactive, claude-code, writes not allowed.
"""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
import queue
import threading
import traceback
from pathlib import Path
from cygnus import paths

REGISTRY_PATH = Path(os.getenv("JARVIS_PROJECTS_FILE", paths.PROJECTS))
# Dev servers are long-lived and chatty; their output goes to a file so a voice
# turn is never blocked by a full pipe buffer.
LOG_PATH = paths.PROJECTS_LOG

BRAVE_EXE = os.getenv(
    "JARVIS_BRAVE_EXE",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)
PROFILE_DIR = Path(os.getenv("JARVIS_PROJECT_PROFILE",
                             Path.home() / ".jarvis-project-profile"))

# How long to wait for a freshly started dev server to answer on its URL.
STARTUP_TIMEOUT = 60
# Login is best-effort by design: if the page has no form we say so and stop
# rather than sitting on a selector until the voice turn dies.
LOGIN_FIELD_TIMEOUT = 4000

USERNAME_SELECTORS = [
    "input[type=email]",
    "input[name=email]",
    "input[id*=email]",
    "input[name=username]",
    "input[id*=username]",
    "input[name*=user]",
]
PASSWORD_SELECTORS = [
    "input[type=password]",
    "input[name=password]",
    "input[id*=password]",
]
SUBMIT_SELECTORS = [
    "button[type=submit]",
    'button:has-text("Login")',
    'button:has-text("Log in")',
    'button:has-text("Sign in")',
    'button:has-text("Sign In")',
    "input[type=submit]",
]

# Written on first use so the user has something real to edit rather than an
# empty file. Paths that do not exist yet are reported, not guessed at.
DEFAULT_REGISTRY = {
    "example-web-app": {
        "path": str(Path.home() / "Documents" / "example-web-app"),
        "dev_cmd": "npm run dev",
        "url": "http://localhost:5173",
        "note": "An example. Your own projects go in data/projects.json.",
    },
}


class BraveProjectWorker:
    """Single thread owning the Brave Playwright context; all calls funnel through it."""

    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._pw = None
        self._ctx = None
        self._page = None

    def call(self, fn, timeout=180):
        """Run fn(page) on the worker thread and return its result."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
        reply = queue.Queue(maxsize=1)
        self._jobs.put((fn, reply))
        try:
            ok, value = reply.get(timeout=timeout)
        except queue.Empty:
            return f"[Error] Project browser call timed out after {timeout}s"
        if not ok:
            return f"[Error] {value}"
        return value

    def _loop(self):
        while True:
            fn, reply = self._jobs.get()
            try:
                page = self._ensure_page()
                reply.put((True, fn(page)))
            except Exception as e:
                traceback.print_exc()
                reply.put((False, f"{type(e).__name__}: {e}"))

    def _ensure_page(self):
        from playwright.sync_api import sync_playwright

        if self._page is not None and not self._page.is_closed():
            return self._page

        if not Path(BRAVE_EXE).exists():
            raise RuntimeError(f"Brave was not found at {BRAVE_EXE}")

        if self._pw is None:
            self._pw = sync_playwright().start()

        if self._ctx is None:
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                executable_path=BRAVE_EXE,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1400, "height": 900},
            )

        pages = [p for p in self._ctx.pages if not p.is_closed()]
        self._page = pages[0] if pages else self._ctx.new_page()
        return self._page

    def is_open(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def shutdown(self):
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._ctx = self._page = self._pw = None


_worker = BraveProjectWorker()


def _first_visible(page, selectors, timeout=LOGIN_FIELD_TIMEOUT):
    """Return the first selector that actually resolves to a visible element."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=max(700, timeout // len(selectors)))
            return el
        except Exception:
            continue
    return None


def load_registry() -> dict:
    """Read projects.json, writing the template first if the file is missing."""
    if not REGISTRY_PATH.exists():
        # config/projects.example.json if it is there, else the built-in one.
        try:
            seed = json.loads(paths.PROJECTS_EXAMPLE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            seed = DEFAULT_REGISTRY
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------ connector --
# Cygnus's own folder is a project too: the one agents work in when no other
# is active. It is never written to projects.json.
CYGNUS_ID = "cygnus"
DEFAULT_AGENT = "claude-code"
_registry_lock = threading.Lock()


def normalize(entry) -> dict:
    """An entry with the connector fields filled in (the file is not changed)."""
    entry = dict(entry or {})
    entry["active"] = bool(entry.get("active", False))
    entry["agent"] = str(entry.get("agent") or DEFAULT_AGENT)
    entry["allow_writes"] = bool(entry.get("allow_writes", False))
    return entry


def save_registry(registry: dict) -> None:
    """Write projects.json whole, through a temp file so a crash leaves the old one."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, REGISTRY_PATH)


def _same_folder(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(a).expanduser().resolve())) == \
            os.path.normcase(str(Path(b).expanduser().resolve()))
    except (OSError, ValueError):
        return False


def project_info(pid: str, entry: dict | None = None) -> dict:
    """One project as the API and the MCP server show it. The vault note name
    is not shown, only that the project has a login."""
    if pid == CYGNUS_ID:
        folder = paths.ROOT
        entry = {"active": False, "agent": DEFAULT_AGENT, "allow_writes": True,
                 "dev_cmd": "", "url": "", "note": "Cygnus's own folder."}
    else:
        entry = normalize(entry if entry is not None else load_registry().get(pid))
        folder = Path(str(entry.get("path") or "")).expanduser()
    exists = bool(str(folder)) and folder.is_dir()
    return {"id": pid, "path": str(folder).replace("\\", "/"), "exists": exists,
            "builtin": pid == CYGNUS_ID, "active": bool(entry.get("active")),
            "agent": entry["agent"], "allow_writes": entry["allow_writes"],
            "dev_cmd": entry.get("dev_cmd") or "", "url": entry.get("url") or "",
            "note": entry.get("note") or "", "test_cmd": entry.get("test_cmd") or "",
            "git": exists and (folder / ".git").exists(),
            "has_login": bool(entry.get("vault_secret"))}


def list_projects() -> list[dict]:
    """Cygnus first, then the connected projects; exactly one is active."""
    registry = load_registry()
    items = [project_info(k, v) for k, v in registry.items() if isinstance(v, dict)]
    me = project_info(CYGNUS_ID)
    me["active"] = not any(i["active"] for i in items)
    return [me] + items


def active_id() -> str:
    for key, entry in load_registry().items():
        if isinstance(entry, dict) and entry.get("active"):
            return key
    return CYGNUS_ID


def find(name: str):
    """The project id `name` means ("sticky brain" is sticky-brain), or None."""
    if (name or "").strip().lower() in (CYGNUS_ID, "jarvis", "yourself", "your own code"):
        return CYGNUS_ID
    return _resolve(name or "", load_registry()) if (name or "").strip() else None


def set_active(name: str) -> dict:
    """Make `name` the project agents work in; "cygnus" clears it."""
    with _registry_lock:
        registry = load_registry()
        pid = CYGNUS_ID if find(name) == CYGNUS_ID else _resolve(name or "", registry)
        if pid is None:
            raise KeyError(name)
        for key, entry in registry.items():
            if isinstance(entry, dict):
                entry["active"] = key == pid
        save_registry(registry)
    return project_info(pid, None if pid == CYGNUS_ID else registry[pid])


def _detect_dev_cmd(folder: Path) -> str:
    """"npm run dev" when package.json has a dev script; nothing is guessed otherwise."""
    try:
        scripts = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return ""
    return "npm run dev" if "dev" in scripts else ""


def _too_broad(folder: Path) -> bool:
    """A drive root or the home folder is not a project: connecting it would let
    agents work across everything."""
    return folder.parent == folder or _same_folder(folder, Path.home())


def connect(path: str, name: str = "", dev_cmd: str | None = None, url: str = "",
            agent: str = "", allow_writes: bool = False, active: bool = False) -> dict:
    """Add a folder to projects.json (or return the entry that has it).
    Raises ValueError with a sentence when the folder cannot be a project."""
    folder = Path(str(path or "").strip().strip('"')).expanduser()
    if not str(path or "").strip():
        raise ValueError("Give the folder to connect.")
    if not folder.is_absolute():
        raise ValueError(f"{path} is not a full folder path.")
    if not folder.is_dir():
        raise ValueError(f"{folder} is not a folder on this computer.")
    folder = folder.resolve()
    if _too_broad(folder):
        raise ValueError(f"{folder} is too broad to connect; pick the project's own folder.")
    if _same_folder(folder, paths.ROOT):
        return set_active(CYGNUS_ID) if active else project_info(CYGNUS_ID)
    with _registry_lock:
        registry = load_registry()
        for key, entry in registry.items():
            if isinstance(entry, dict) and _same_folder(entry.get("path") or "", folder):
                pid = key
                break
        else:
            base = re.sub(r"[^a-z0-9]+", "-", (name or folder.name).lower()).strip("-") or "project"
            pid, n = base, 2
            while pid in registry or pid == CYGNUS_ID:
                pid, n = f"{base}-{n}", n + 1
            registry[pid] = {"path": str(folder).replace("\\", "/"),
                             "dev_cmd": _detect_dev_cmd(folder) if dev_cmd is None else dev_cmd,
                             "url": url or "", "agent": agent or DEFAULT_AGENT,
                             "allow_writes": bool(allow_writes), "active": False}
        if active:
            for key, entry in registry.items():
                if isinstance(entry, dict):
                    entry["active"] = key == pid
        save_registry(registry)
    return project_info(pid, registry[pid])


def disconnect(name: str) -> str:
    """Forget a project. Its folder is not touched. Returns its id."""
    with _registry_lock:
        registry = load_registry()
        pid = _resolve(name or "", registry)
        if pid is None or pid == CYGNUS_ID:
            raise KeyError(name)
        registry.pop(pid)
        save_registry(registry)
    return pid


def writes_allowed(pid: str) -> bool:
    if pid == CYGNUS_ID:
        return True
    return normalize(load_registry().get(pid)).get("allow_writes", False)


def allow_writes(pid: str, allowed: bool = True) -> None:
    """The owner said yes (or no) to agents changing files in `pid`."""
    if pid == CYGNUS_ID:
        return
    with _registry_lock:
        registry = load_registry()
        if isinstance(registry.get(pid), dict):
            registry[pid]["allow_writes"] = bool(allowed)
            save_registry(registry)


def _named_in(task: str, registry: dict):
    """The connected project a task names outright ("fix the header in
    sticky-brain", "... in sticky brain"), as a whole word, or None."""
    low = f" {(task or '').lower()} "
    for key in sorted(registry, key=len, reverse=True):     # "my-app-2" before "my-app"
        spoken = re.sub(r"[-_]+", " ", key.lower())
        for form in {key.lower(), spoken}:
            if re.search(rf"(?<![\w-]){re.escape(form)}(?![\w-])", low):
                return key
    return None


def project_for(task=None) -> tuple[str, Path]:
    """(project id, folder) an agent working on `task` runs in: the project the
    task names, else the active one, else Cygnus's own folder. A project whose
    folder is gone falls back to Cygnus's folder rather than failing the spawn."""
    if isinstance(task, dict):
        task = task.get("project") or task.get("task") or ""
    registry = {k: v for k, v in load_registry().items() if isinstance(v, dict)}
    pid = _named_in(str(task or ""), registry) or next(
        (k for k, v in registry.items() if v.get("active")), None)
    if pid:
        folder = Path(str(registry[pid].get("path") or "")).expanduser()
        if str(folder) and folder.is_dir():
            return pid, folder
        print(f"[projects] {pid} points at {folder}, which is missing; using Cygnus's folder", flush=True)
    return CYGNUS_ID, paths.ROOT


def git_head(folder) -> str:
    """The folder's current commit, or "" outside git."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(folder),
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def record_job(jid: str, pid: str, folder) -> None:
    """Put the project, and the commit it was at, on the job: "what did it
    change?" diffs from there."""
    from cygnus.jobs import registry as jobreg
    jobreg.update(jid, project=pid, project_head=git_head(folder))


# --------------------------------------------------------------- voice --
# "work on sticky brain", "switch to my-app", "back to your own code",
# "connect this folder", "connect C:\code\app and work on it",
# "which project are we on?". Instant and local (brain/core.py).
_LEAD = r"^(?:(?:ok(?:ay)?|now|so|please|hey|cygnus|jarvis|alright)[,\s]+)*"
_WORK_ON_RE = re.compile(
    _LEAD + r"(?:let'?s\s+|let\s+us\s+|i\s+want\s+to\s+|i'?m\s+going\s+to\s+|we'?re\s+going\s+to\s+)?"
    r"(?:work\s+(?:on|in)|switch\s+(?:over\s+)?(?:to|back\s+to)|focus\s+on|go\s+back\s+to|back\s+to)\s+"
    r"(?:the\s+|my\s+|our\s+)?(.+?)(?:\s+(?:project|repo|repository|folder|codebase))?"
    r"(?:\s+(?:now|please|instead))?\s*[.!?]*$", re.I)
_CONNECT_RE = re.compile(
    _LEAD + r"(?:connect|add|link|hook\s+up)\s+(?:(?:this|the\s+current|the\s+open|that)\s+folder\b|"
    r"(?:the\s+)?folder\s+[A-Za-z]:[\\/]|[A-Za-z]:[\\/])", re.I)
_WHICH_RE = re.compile(
    _LEAD + r"(?:(?:what|which)\s+project\s+(?:are\s+(?:we|you)|am\s+i|is)\s+(?:on|in|active|working)"
    r"|what\s+are\s+(?:we|you)\s+working\s+on\s+(?:now|right\s+now)?\s*\??$)", re.I)
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\"<>|?*\n]*")


def _spoken_match(target: str):
    """The project `target` names exactly ("sticky brain" is sticky-brain),
    or None. Stricter than _resolve: "work on the report" must not pick a
    project whose name happens to contain "report"."""
    t = re.sub(r"^(?:the|my|our)\s+", "", (target or "").strip().lower())
    if t in (CYGNUS_ID, "jarvis", "yourself", "your own code", "your code", "your own project",
             "you", "your own folder"):
        return CYGNUS_ID
    flat = re.sub(r"[^a-z0-9]+", "", t)
    if not flat:
        return None
    for key in load_registry():
        if re.sub(r"[^a-z0-9]+", "", key.lower()) == flat:
            return key
    return None


def voice_intent(text: str):
    """"project_switch", "project_connect", "project_which" or None."""
    t = (text or "").strip()
    if _WHICH_RE.match(t):
        return "project_which"
    if _CONNECT_RE.match(t):
        return "project_connect"
    m = _WORK_ON_RE.match(t)
    if m:
        try:
            if _spoken_match(m.group(1)):
                return "project_switch"
        except Exception:
            return None
    return None


def explorer_folder():
    """The folder shown in the front-most File Explorer window, or None."""
    if os.name != "nt":
        return None
    ps = ("$s = New-Object -ComObject Shell.Application; foreach ($w in $s.Windows()) { try { "
          "$p = $w.Document.Folder.Self.Path; if ($p) { '{0}|{1}' -f $w.HWND, $p } } catch {} }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    windows = {}
    for line in (out or "").splitlines():
        hwnd, _, folder = line.strip().partition("|")
        if hwnd.isdigit() and folder and Path(folder).is_dir():
            windows[int(hwnd)] = folder
    if not windows:
        return None
    try:                                         # top of the z-order first
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetTopWindow.restype = user32.GetWindow.restype = ctypes.c_void_p
        user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        h = user32.GetTopWindow(None)
        while h:
            if h in windows:
                return windows[h]
            h = user32.GetWindow(h, 2)           # GW_HWNDNEXT
    except Exception:
        pass
    return next(iter(windows.values()))


def _said_folder(said: str) -> str:
    """The folder in "C:\\code\\my app and work on it": folder names may have
    spaces, so the longest leading run of words that is a folder wins."""
    words = said.strip().split(" ")
    for n in range(len(words), 0, -1):
        cand = " ".join(words[:n]).strip().rstrip(".,;!?")
        if cand and Path(cand).is_dir():
            return cand
    return said.strip().rstrip(".,;!?")


def describe(info: dict) -> str:
    return f"{info['id']} ({info['path']})"


def voice(text: str):
    """The spoken reply to a project command, or None when `text` is not one."""
    kind = voice_intent(text)
    if kind == "project_which":
        pid = active_id()
        if pid == CYGNUS_ID:
            return "We're on my own code, sir. Say \"work on\" and a project to switch."
        return f"We're working on {describe(project_info(pid))}, sir."
    if kind == "project_switch":
        pid = _spoken_match(_WORK_ON_RE.match(text.strip()).group(1))
        info = set_active(pid)
        if pid == CYGNUS_ID:
            return "Back on my own code, sir. Agents will work in Cygnus's folder."
        if not info["exists"]:
            return (f"{pid} is active, sir, but {info['path']} is missing, so agents will "
                    "work in my own folder until it's back.")
        writes = "" if info["allow_writes"] else " I'll ask before an agent changes anything there."
        return f"Working on {describe(info)}, sir. Coding tasks will run in that folder.{writes}"
    if kind == "project_connect":
        m = _PATH_RE.search(text)
        folder = _said_folder(m.group(0)) if m else explorer_folder()
        if not folder:
            return ("I can't see which folder you mean, sir. Open it in File Explorer and say "
                    "\"connect this folder\" again, or say its full path.")
        make_active = bool(re.search(r"\b(?:work\s+on\s+it|make\s+it\s+active|switch\s+to\s+it|"
                                     r"and\s+use\s+it)\b", text, re.I))
        try:
            info = connect(folder, active=make_active)
        except ValueError as e:
            return f"I couldn't connect that, sir. {e}"
        if info["builtin"]:
            return "That's my own folder, sir; it's always connected."
        tail = (" and made it the active project" if info["active"] else
                f". Say \"work on {info['id'].replace('-', ' ')}\" to switch to it")
        return (f"Connected {describe(info)}{tail}, sir. Agents will ask before changing files "
                "there the first time.")
    return None


def _resolve(name: str, registry: dict):
    """Match a spoken project name against the registry keys, loosely."""
    needle = name.lower().strip()
    for key in registry:
        if key.lower() == needle:
            return key
    # Spoken names lose hyphens ("sticky brain"), so compare on letters alone.
    flat = "".join(ch for ch in needle if ch.isalnum())
    for key in registry:
        keyflat = "".join(ch for ch in key.lower() if ch.isalnum())
        if keyflat == flat or flat in keyflat or keyflat in flat:
            return key
    return None


def _url_alive(url: str, timeout: float = 2.0) -> bool:
    """True if anything at all answers on the URL. A 404 still means it is up."""
    try:
        urllib.request.urlopen(url, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _wait_for_url(url: str, timeout: int = STARTUP_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _url_alive(url):
            return True
        time.sleep(1.0)
    return False


def _start_dev_server(name: str, cmd: str, cwd: Path) -> str:
    """Spawn the dev command detached, with its output appended to projects.log."""
    log = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    log.write(f"\n=== {name}: {cmd} in {cwd} at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log.flush()
    subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=log,
                     stderr=subprocess.STDOUT)
    return f"started '{cmd}' (output in {LOG_PATH.name})"


def _open_and_login(url: str, creds) -> str:
    """Open the URL in Brave and, if we have credentials, try a generic login.

    Best-effort throughout: a missing form, a missing field or a missing button
    each end the attempt with a plain report instead of a wait. The password is
    typed into the page and never returned or printed.
    """

    def job(page):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        try:
            page.bring_to_front()
        except Exception:
            pass

        if not creds:
            return f"opened {url} in Brave"

        pw_field = _first_visible(page, PASSWORD_SELECTORS)
        if pw_field is None:
            return (f"opened {url} in Brave but could not auto-login - no password "
                    "field appeared, so it may already be signed in or the login "
                    "page is elsewhere")

        user_field = _first_visible(page, USERNAME_SELECTORS, timeout=2500)
        filled = []
        if user_field is not None and creds.get("username"):
            user_field.fill(creds["username"])
            filled.append("username")
        if creds.get("password"):
            pw_field.fill(creds["password"])
            filled.append("password")
        if not filled:
            return f"opened {url} in Brave but the credential note had nothing to fill in"

        submit = _first_visible(page, SUBMIT_SELECTORS, timeout=2500)
        if submit is None:
            return (f"opened {url} in Brave and filled the {' and '.join(filled)}, "
                    "but found no submit button - press enter there yourself")
        submit.click()
        page.wait_for_timeout(3000)
        return f"opened {url} in Brave and submitted the login as {creds.get('username')}"

    return _worker.call(job, timeout=140)


def _open_plainly(url: str, why: str) -> str:
    """Last resort: hand the URL to Brave through the OS, and say that we did."""
    try:
        if Path(BRAVE_EXE).exists():
            subprocess.Popen([BRAVE_EXE, url])
        else:
            webbrowser.open(url)
        return f"opened {url} in a normal Brave window ({why})"
    except Exception as e:
        return f"could not open {url}: {why}; launching Brave also failed: {e}"


def launch_project(name: str) -> str:
    """Start a project's dev server, wait for it, open it in Brave, maybe log in.

    Everything is wrapped so a broken registry entry, a dead server or a browser
    failure comes back as a sentence rather than taking the turn down with it.
    """
    try:
        registry = load_registry()
    except Exception as e:
        return f"[Error] Could not read {REGISTRY_PATH.name}: {e}"

    try:
        key = _resolve(name, registry)
        if key is None:
            listed = ", ".join(registry) or "none"
            return f"[Error] No project called '{name}'. I know about: {listed}"

        entry = registry[key] or {}
        raw_path = (entry.get("path") or "").strip()
        dev_cmd = (entry.get("dev_cmd") or "").strip()
        url = (entry.get("url") or "").strip()
        secret = (entry.get("vault_secret") or "").strip()

        if not raw_path:
            hint = entry.get("note") or "Add its path to projects.json."
            return f"[Error] {key} has no path set yet. {hint}"
        path = Path(raw_path).expanduser()
        if not path.exists():
            return f"[Error] {key} points at {path}, which does not exist."

        steps = []

        already_up = bool(url) and _url_alive(url)
        if dev_cmd and already_up:
            steps.append(f"{url} was already answering, so I left the server alone")
        elif dev_cmd:
            steps.append(_start_dev_server(key, dev_cmd, path))
            if url:
                if _wait_for_url(url):
                    steps.append(f"{url} came up")
                else:
                    return (f"Started {key} but {url} did not answer within "
                            f"{STARTUP_TIMEOUT} seconds. Check {LOG_PATH.name}.")
        elif not url:
            return (f"Opened nothing for {key}: it has no dev command and no URL "
                    f"in {REGISTRY_PATH.name}. Its folder is {path}.")

        creds = None
        if secret:
            from cygnus import tools
            got = tools.get_credentials(secret)
            if got.get("ok"):
                creds = got
                steps.append(f"credentials from the vault - {got['summary']}")
            else:
                steps.append(f"no credentials used - {got.get('summary')}")

        if url:
            opened = _open_and_login(url, creds)
            if isinstance(opened, str) and opened.startswith("[Error]"):
                opened = _open_plainly(url, opened[len("[Error]"):].strip())
            steps.append(opened)

        return f"{key}: " + "; ".join(steps) + "."
    except Exception as e:
        return f"[Error] Could not launch '{name}': {type(e).__name__}: {e}"


if __name__ == "__main__":
    # python project_agent.py  ->  shows the registry without launching anything.
    reg = load_registry()
    print("Registry:", REGISTRY_PATH)
    for k, v in reg.items():
        print(f"  {k}: path={v.get('path') or '(unset)'} "
              f"dev_cmd={v.get('dev_cmd') or '(none)'} url={v.get('url') or '(none)'} "
              f"vault_secret={v.get('vault_secret') or '(none)'}")
