# System Tools - File ops, shell commands, web search
import os
import shutil
import subprocess
import threading
import json
import platform
import re
import time
import datetime
import difflib
import webbrowser
import urllib.parse
import urllib.request
from pathlib import Path
from cygnus import paths

# Phase 1: Structured audit trail (load-bearing before concurrency).
# Every delegation + every file read/write lands here as JSONL: timestamp,
# caller, args (redacted), result (truncated), confirm-gate decision. Stdlib only.
_AUDIT_PATH = paths.LOG_DIR / "audit.jsonl"
_AUDIT_LOCK = threading.Lock()
_AUDIT_SECRET_RE = re.compile(
    r"""(?i)([A-Z0-9_-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_-]*)(["']?\s*[=:]\s*)(["']?)([^\s"',;]+)"""
)

def _audit_log(caller: str, task: str, decision: str, result: str = "", confirm: bool = False, extra: dict = None):
    """Append one structured audit entry. Never raises; audit must not break a turn."""
    try:
        rec = {
            "ts": datetime.datetime.now().isoformat(timespec="milliseconds"),
            "caller": caller,
            "task": (task or "")[:500],
            "decision": decision,
            "confirm": bool(confirm),
            "result": (result or "")[:800],
        }
        if extra:
            rec.update(extra)
        # redact secrets in task/result
        for k in ("task", "result"):
            if rec[k]:
                rec[k] = _AUDIT_SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", rec[k])
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

# How much of the audit trail's end is read: recent writes, and a search for
# writes a question names (older, so further back).
_AUDIT_TAIL_BYTES = 256 * 1024
_AUDIT_SEARCH_BYTES = 4 * 1024 * 1024


def recent_file_writes(hours: float = 2.0, limit: int = 8, match=None) -> list:
    """Files JARVIS itself wrote with write_file, oldest first, as "HH:MM
    path": those of the last `hours`, or - given `match` words - those whose
    path holds one of them, however old ("hello world website" finds
    hello-world-website\\index.html from the morning)."""
    try:
        with open(_AUDIT_PATH, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - (_AUDIT_SEARCH_BYTES if match else _AUDIT_TAIL_BYTES)))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=hours)
    words = [w.lower() for w in (match or [])]
    out = []
    for line in tail:
        if '"write_file"' not in line:
            continue
        try:
            rec = json.loads(line)
            ts = datetime.datetime.fromisoformat(rec["ts"])
        except (ValueError, KeyError, TypeError):
            continue
        path = str(rec.get("task", ""))
        if rec.get("caller") != "write_file" or rec.get("decision") != "executed":
            continue
        if (any(w in path.lower() for w in words) if words else ts >= cutoff):
            out.append(f"{ts:%H:%M} {path}")
    return out[-limit:]

def _quarantine_external(text: str, label: str = "external") -> str:
    """Wrap external text (browser/chat/file) as data, quarantining instruction-like spans.
    
    Uses guard.sanitize_web_text() so downstream models treat it as data, not directions.
    Logs quarantine hits to audit trail.
    """
    if not text:
        return text
    try:
        from cygnus.browser import guard
        cleaned = guard.sanitize_web_text(text, label=label)
        if cleaned != text and guard.is_suspicious(text):
            _audit_log("guard", f"quarantine:{label}", "quarantined", f"hits in {label}", extra={"label": label})
        return cleaned
    except Exception:
        return text

# Thread-local progress callback — set by brain_gemini.think() so tools can
# stream mid-task status ("Searching YouTube Music...") back to the WS layer.
_tools_tls = threading.local()


def set_progress_cb(cb):
    """Store the progress callback for the current thread (called by brain)."""
    _tools_tls.cb = cb


def set_job_callback(cb):
    """Store where a background job's late answer goes for this thread's turn
    (called by brain); None when the turn can only answer now."""
    _tools_tls.on_done = cb


def _job_run(kw: dict) -> dict:
    """background/on_done for a delegation the model starts: in the background
    whenever the turn can take a late answer. Without it a Hermes run held
    the model's turn for up to five minutes."""
    on_done = kw.get("on_done") or getattr(_tools_tls, "on_done", None)
    return {"background": _truthy(kw.get("background", False)) or callable(on_done), "on_done": on_done}


def progress(msg: str):
    """Emit a mid-task status update if a callback is set. Safe to call anytime."""
    cb = getattr(_tools_tls, "cb", None)
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def get_system_info():
    """Get basic system information."""
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cwd": os.getcwd(),
        "user": os.getenv("USER") or os.getenv("USERNAME", "unknown"),
        "time": datetime.datetime.now().isoformat(),
    }


def run_shell(command: str) -> str:
    """Execute a shell command and return output."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
            if result.stderr:
                output += f"\n{result.stderr.strip()}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "[Error] Command timed out after 30s"
    except Exception as e:
        return f"[Error] {str(e)}"


# Phase 7: Scoped file access — only these roots are readable/writable without extra confirm.
# Writes outside these require allowlist confirm; reads outside are blocked and logged.
_ALLOWED_ROOTS = [
    Path.home() / "Documents",
    Path.home() / "Desktop",
    Path.home() / "Downloads",
    Path.home() / "Documents" / "Second Brain",
    Path.home() / "Documents" / "Portfolio",
    paths.ROOT,  # the project folder itself (configs, logs) — scoped
]
# Its name in the blocked-path replies ("CYGNUS"), so they name the real folder.
_PROJECT_NAME = paths.ROOT.name

def _is_allowed_path(p: Path) -> bool:
    """True if p is under any allowed root (resolved, case-insensitive on Windows)."""
    try:
        rp = p.resolve()
        for root in _ALLOWED_ROOTS:
            try:
                if rp.is_relative_to(root.resolve()):
                    return True
                # Windows case-insensitive prefix check
                if str(rp).lower().startswith(str(root.resolve()).lower()):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False

def read_file(path: str) -> str:
    """Read a file's contents, scoped to allowed roots, quarantined, audited."""
    try:
        p = Path(path).expanduser()
        # A relative path is the user's ("Desktop/x.txt" is in their Desktop):
        # it was checked that way but read relative to the server's folder,
        # which showed the model Cygnus's own files (2026-09-22).
        p = p if p.is_absolute() else _resolve_write_path(str(p))
        # Scope check before touching disk (Phase 7)
        if not _is_allowed_path(p):
            _audit_log("read_file", str(p), "blocked_scope", result="outside allowed roots")
            return f"[Blocked] Path outside allowed scope: {p}. Allowed: Documents, Desktop, Downloads, Second Brain, Portfolio, or {_PROJECT_NAME}."
        if not p.exists():
            _audit_log("read_file", str(p), "not_found")
            return f"[Error] File not found: {path}"
        if p.is_dir():
            return f"[Error] That is a directory, not a file: {path}"
        if p.stat().st_size > 1_000_000:
            return "[Error] File too large (>1MB)"

        raw = p.read_bytes()
        if b"\x00" in raw[:4096]:
            return (f"[Error] {p.name} is a binary file, not text. I can only read "
                    "plain text files - PDFs, Word documents, images and archives "
                    "need a converter I do not have.")
        for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
        # Phase 1b: quarantine external file content before it enters model context
        q = _quarantine_external(text, label=f"file:{p.name}")
        _audit_log("read_file", str(p), "executed", result=f"{len(q)} chars", extra={"allowed": True})
        return q
    except Exception as e:
        _audit_log("read_file", str(path), "error", result=str(e))
        return f"[Error] {str(e)}"


# A bare filename used to resolve against the server's working directory, which
# is the JARVIS source folder — "save my notes as notes.txt" buried the file
# among the .py files where nobody would look for it. Spoken requests almost
# never carry a directory, so an unqualified name belongs somewhere the user
# actually opens.
DEFAULT_WRITE_DIR = Path.home() / "Documents"


# Folder names the model commonly prefixes ("Documents/notes.txt"). These are
# meant relative to the user's home, never to the server's working directory.
_USER_FOLDERS = {"documents", "desktop", "downloads", "pictures", "music",
                 "videos", "onedrive"}


def _resolve_write_path(path: str) -> Path:
    """Expand `path`, keeping relative writes out of the JARVIS source folder.

    Absolute paths are honoured as given. Everything else is anchored to the
    user's own folders: a bare name goes to Documents, and a path that already
    starts with a known user folder ("Documents/notes.txt") is joined onto the
    home directory. Resolving those against the CWD produced files nested at
    jarvis-demo\\Documents\\ while JARVIS told the user they were in Documents.
    """
    p = Path(str(path)).expanduser()
    if p.is_absolute():
        return p
    parts = [q for q in p.parts if q not in (".", "")]
    if not parts:
        return DEFAULT_WRITE_DIR
    if parts[0].lower() in _USER_FOLDERS:
        return Path.home().joinpath(*parts)
    if len(parts) > 1:
        return DEFAULT_WRITE_DIR.joinpath(*parts)
    return DEFAULT_WRITE_DIR / parts[0]


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """Write content to a file, scoped and audited (Phase 7). Overwrite needs confirm via allowlist."""
    try:
        p = _resolve_write_path(path)
        # Phase 7: scope check — writes must be under allowed roots
        if not _is_allowed_path(p):
            _audit_log("write_file", str(p), "blocked_scope", result="outside allowed roots", extra={"overwrite": overwrite})
            return f"[Blocked] Write outside allowed scope: {p}. Allowed: Documents, Desktop, Downloads, Second Brain, Portfolio, {_PROJECT_NAME}."
        if p.exists() and not overwrite:
            size = p.stat().st_size
            _audit_log("write_file", str(p), "blocked_exists", extra={"size": size, "overwrite": overwrite})
            return (f"[Blocked] {p.name} already exists ({size:,} bytes) and I did not "
                    "overwrite it. Confirm you want it replaced and I will.")
        # Phase 1c+7: audit every write (args redacted, content truncated)
        _audit_log("write_file", str(p), "executed" if not p.exists() or overwrite else "blocked", result=f"{len(content)} chars", extra={"overwrite": overwrite, "allowed": True})
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        # quarantine content that looks like embedded instruction? No — this is outbound, but log if suspicious
        try:
            from cygnus.browser import guard
            if guard.is_suspicious(content):
                _audit_log("write_file", str(p), "suspicious_content_quarantined", result=content[:200])
        except Exception:
            pass
        p.write_text(content, encoding="utf-8")
        verb = "Replaced" if existed else "Written to"
        return f"{verb} {p} ({len(content)} chars)"
    except Exception as e:
        _audit_log("write_file", str(path), "error", result=str(e))
        return f"[Error] {str(e)}"


# Bounds so a search rooted at a home directory cannot stall a voice turn.
SEARCH_TIME_BUDGET = 20.0
SEARCH_MAX_VISITED = 40_000
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "AppData",
    "site-packages", "Windows", "Program Files", "Program Files (x86)",
    "$Recycle.Bin", "OneDrive", "dist", "build",
}

_SECRET_LINE = re.compile(
    # Key name may be quoted (JSON) or bare (dotenv); value likewise.
    r"""(?i)([A-Z0-9_-]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_-]*)"""
    r"""(["']?\s*[=:]\s*)(["']?)([^\s"',]+)"""
)


def _redact(line: str) -> str:
    """Blank out secret values so search results never speak a key aloud."""
    return _SECRET_LINE.sub(lambda m: f"{m.group(1)}=<redacted>", line)


def search_files(query: str, path: str = ".", max_results: int = 20) -> str:
    """Search file contents recursively — scoped to allowed roots (Phase 7), quarantined, audited."""
    try:
        root = Path(path).expanduser()
        check_root = root if root.is_absolute() else (_resolve_write_path(str(root)) if str(root) not in (".", "") else Path.home() / "Documents")
        root = check_root     # search what was checked (see read_file)
        if not _is_allowed_path(check_root):
            _audit_log("search_files", str(root), "blocked_scope")
            return f"[Blocked] Path outside allowed scope: {root}. Allowed: Documents, Desktop, Downloads, Second Brain, Portfolio."
        _audit_log("search_files", str(root), "executed", extra={"query": query[:100]})
        if not root.exists():
            return f"[Error] Path not found: {path}"

        needle = query.lower()
        hits = []
        scanned = 0
        visited = 0
        deadline = time.monotonic() + SEARCH_TIME_BUDGET
        stopped = None

        # Streamed, not materialised: rglob on a home directory would build a list
        # of hundreds of thousands of paths before a single line was ever matched,
        # which stalls the whole voice turn.
        walker = [root] if root.is_file() else root.rglob("*")
        for f in walker:
            visited += 1
            if visited > SEARCH_MAX_VISITED:
                stopped = f"stopped after walking {SEARCH_MAX_VISITED:,} paths"
                break
            if time.monotonic() > deadline:
                stopped = f"stopped after {SEARCH_TIME_BUDGET}s"
                break
            if any(part in SKIP_DIRS or part.startswith(".") for part in f.parts[-4:]):
                continue
            try:
                if not f.is_file() or f.stat().st_size > 2_000_000:
                    continue
                raw = f.read_bytes()
                if b"\x00" in raw[:2048]:
                    continue
                text = raw.decode("utf-8", "replace")
            except Exception:
                continue
            scanned += 1
            for n, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{f.name}:{n}: {_redact(line.strip())[:120]}")
                    break
            if len(hits) >= max_results:
                stopped = f"stopped at the {max_results}-result limit"
                break

        note = f" (search {stopped})" if stopped else ""
        if not hits:
            return f"No matches for '{query}' in {scanned} text files under {root}.{note}"
        return (f"Found '{query}' in {len(hits)} of {scanned} files searched{note}: "
                + " | ".join(hits))
    except Exception as e:
        return f"[Error] {str(e)}"


def list_directory(path: str = ".") -> str:
    """List directory contents — scoped to allowed roots (Phase 7), audited."""
    try:
        p = Path(path).expanduser()
        # Resolve relative to allowed roots: bare "." means Documents for voice safety
        check_p = p if p.is_absolute() else (_resolve_write_path(str(p)) if str(p) not in (".", "") else Path.home() / "Documents")
        p = check_p           # list what was checked (see read_file)
        if not _is_allowed_path(check_p):
            _audit_log("list_directory", str(p), "blocked_scope")
            return f"[Blocked] Path outside allowed scope: {p}. Allowed: Documents, Desktop, Downloads, Second Brain, Portfolio."
        _audit_log("list_directory", str(p), "executed")
        items = []
        for item in sorted(p.iterdir()):
            prefix = "[dir]" if item.is_dir() else "[file]"
            size = item.stat().st_size if item.is_file() else 0
            items.append(f"{prefix} {item.name} ({size:,} bytes)" if size else f"{prefix} {item.name}/")
        return "\n".join(items) or "(empty directory)"
    except Exception as e:
        _audit_log("list_directory", str(path), "error", result=str(e))
        return f"[Error] {str(e)}"


# Moved to vault.py (restructure phase 5); re-exported so
# `tools.<name>` keeps working for callers and tests.
from cygnus.tools.vault import (  # noqa: E402,F401
    _clean_cell,
    _DEFAULT_EMBEDDING_MODEL,
    _extra_models,
    _get_credentials_tool,
    _get_embedding_model,
    _load_turbovec_index,
    _normalize_model_name,
    _parse_credential_table,
    _PASSWORD_KEYS,
    _turbovec_cache,
    _turbovec_cache_vault,
    _USERNAME_KEYS,
    _vault_notes,
    _vault_text,
    get_credentials,
    read_vault_note,
    search_vault,
    search_vault_semantic,
    VAULT_ROOT,
    VAULT_SECRETS_DIR,
    VAULT_SKIP_DIRS,
    VAULT_SKIP_PREFIXES,
)


# --- Open a file by name ------------------------------------------------------
OPEN_FILE_ROOTS = [
    Path(r"C:\Users\deped\Documents"),
    Path(r"C:\Users\deped\Desktop"),
    Path(r"C:\Users\deped\Downloads"),
    Path(r"C:\Users\deped\Documents\Second Brain"),
    Path(r"C:\Users\deped\Documents\Portfolio"),
]
# Spoken requests almost always mean a document, not a stray .tmp beside it.
DOCUMENT_SUFFIXES = (".pptx", ".ppt", ".docx", ".doc", ".pdf", ".xlsx", ".xls",
                     ".md", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".mp4")


def find_file(name: str):
    """Return (best_match_path_or_None, candidate_paths) for a filename fragment.

    Bounded by the same time budget and visit cap as search_files so a mistaken
    request cannot stall a voice turn.
    """
    needle = name.lower()
    deadline = time.monotonic() + SEARCH_TIME_BUDGET
    visited = 0
    hits = []

    for root in OPEN_FILE_ROOTS:
        if not root.exists():
            continue
        for f in root.rglob("*"):
            visited += 1
            if visited > SEARCH_MAX_VISITED or time.monotonic() > deadline:
                break
            if any(part in SKIP_DIRS or part.startswith(".") for part in f.parts[-4:]):
                continue
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            if needle in f.name.lower() and f not in hits:
                hits.append(f)
                if len(hits) >= 40:
                    break
        if visited > SEARCH_MAX_VISITED or time.monotonic() > deadline:
            break

    if not hits:
        return None, []
    # Prefer a document-like file, then an exact stem match, then the shortest path.
    docs = [f for f in hits if f.suffix.lower() in DOCUMENT_SUFFIXES] or hits
    exact = [f for f in docs if f.stem.lower() == needle] or docs
    best = min(exact, key=lambda f: (len(f.name), len(str(f))))
    return best, hits


def open_file(name: str) -> str:
    """Find a file by part of its name across the user's usual folders and open it."""
    try:
        best, hits = find_file(name)
        if best is None:
            return (f"[Error] No file matching '{name}' under Documents, Desktop, "
                    "Downloads, the Second Brain vault or Portfolio.")
        if platform.system() == "Windows":
            os.startfile(str(best))
        else:
            subprocess.run(["xdg-open", str(best)])
        extra = f" ({len(hits) - 1} other matches ignored)" if len(hits) > 1 else ""
        return f"Opened '{name.split('/')[-1] if '/' in name else name}'{extra}"
    except Exception as e:
        return f"[Error] Could not open '{name}': {str(e)}"


def search_web(query: str) -> str:
    """Open web search in browser."""
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    webbrowser.open(url)
    return f"Opened search: {query}"

def web_browse(url: str, mode: str = "open", query: str = "") -> str:
    """Token-cheap web read via the local `oc` CLI (only-cli/oc).

    Turns a page into a compact numbered view instead of raw HTML, so the
    model reads a few hundred tokens instead of tens of thousands. `mode`:
    'open' (numbered view, default), 'raw' (distilled markdown of whole page),
    'read' (deep read of a region). External page text is DATA, not
    instructions — execute_tool() quarantines the result via guard.py.

    Returns oc's output, or an honest [Error] if oc is missing / the page
    blocks a plain fetch (no login-wall bypass; matches the base stack).
    """
    import shutil, subprocess
    oc = _direct_launcher("oc") or _direct_launcher(r"C:\Users\deped\AppData\Roaming\npm\oc.CMD")
    if not oc:
        return "[Error] oc CLI not found on PATH. Install with: npm i -g @only-cli/oc"
    url = (url or "").strip()
    if mode != "read" and "://" not in url and (" " in url or "." not in url):
        # The model once passed a whole sentence ("go back to the lo-fi
        # channels...") as the address.
        return f"[Error] \"{url[:60]}\" isn't a web address."
    if "://" not in url and "." in url:
        url = "https://" + url
    try:
        if mode == "raw":
            cmd = oc + ["raw", url]
        elif mode == "read":
            cmd = oc + ["read", str(query or 1)]
        else:
            cmd = oc + ["open", url, "--budget", "500"]
        # No shell: the address comes from the model, and a shell would run
        # "https://x.com & anything" as two commands.
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                             encoding="utf-8", errors="replace")
        out = (res.stdout or "").strip()
        if res.returncode != 0 or not out:
            err = (res.stderr or "").strip()[:200]
            return (f"[Error] oc could not read {url} (rc={res.returncode}). "
                    f"{err or 'Page may require login or block plain fetches.'}")
        return out
    except Exception as e:
        return f"[Error] web_browse failed: {e}"


# ---- Web registry (Phase 8): named site resolution ------------------------

# A bare host the user actually spoke as a URL: text on both sides of every
# dot and a TLD-like suffix, or a www. prefix. "holly." (sentence period) and
# "holly" are site NAMES, never https://holly.
_BARE_HOST_RE = re.compile(
    r"(?:www\.[\w-]+(?:\.[\w-]+)*|[\w-]+(?:\.[\w-]+)*\.[a-z]{2,24})"
    r"(?::\d+)?(?:/\S*)?", re.I)
_RULE_URL_RE = re.compile(
    r"https?://[^\s'\"<>]+|(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,24}"
    r"(?:/[^\s'\"<>]*)?", re.I)
_RULE_VERB_RE = re.compile(
    r"^(?:please\s+|(?:jarvis|cygnus)\s+)*(?:visit|go\s+to|take\s+me\s+to|open|launch)\s+"
    r"(?:the\s+|my\s+)?", re.I)


def _norm_phrase(s: str) -> str:
    # "visit hd movies on brave" and a spoken "HD movies" (browser already
    # stripped by the caller) must normalize to the same "hd movies".
    s = re.sub(r"[^\w.\s-]", " ", (s or "").lower())
    s = re.sub(r"\s+", " ", s).strip(" .")
    s = re.sub(r"\s+(?:on|in|with)\s+(?:the\s+)?(?:chrome|brave|edge|firefox|browser)"
               r"(?:\s+browser)?$", "", s)
    s = re.sub(r"\s+(?:site|website)$", "", s)
    return _RULE_VERB_RE.sub("", s).strip()


def _load_app_registry() -> dict:
    try:
        with open(paths.APP_REGISTRY, encoding="utf-8") as f:
            return json.load(f).get("apps", {})
    except Exception:
        return {}


# Spoken browser word -> (label, app_registry keys to try in order). Edge is
# scanned as msedge; the display names are fallbacks for other scans.
_BROWSERS = {"brave": ("Brave", ("brave",)),
             "chrome": ("Chrome", ("chrome", "google chrome")),
             "edge": ("Edge", ("msedge", "microsoft edge", "edge")),
             "firefox": ("Firefox", ("firefox", "mozilla firefox"))}
_CHROME_KEYS = {"chrome", "google chrome"}
_BROWSER_SUFFIX_RE = re.compile(
    r"\s+(?:on|in)\s+(?:the\s+)?(chrome|brave|edge|firefox)(?:\s+browser)?\s*[.!?]*$",
    re.I)
# Same phrase mid-sentence ("search me a movie on Brave named X"). A bare
# "edge" needs "browser" after it here — "in edge cases" is not a browser.
_BROWSER_ANY_RE = re.compile(
    r"\s+(?:on|in)\s+(?:the\s+)?(chrome|brave|firefox|edge(?=\s+browser))"
    r"(?:\s+browser)?\b[.!?,]*", re.I)
_CHAIN_WORD_RE = re.compile(r"\b(?:and|then|also)\b", re.I)


def _split_browser(text: str):
    """(text without its 'on|in <browser>' phrase, browser word or None).

    Trailing phrase first (the original behavior); otherwise the phrase
    anywhere mid-sentence, unless a chained ask follows it ('open youtube
    in chrome and play lofi' is not a single open).
    """
    text = text or ""
    m = _BROWSER_SUFFIX_RE.search(text)
    if not m:
        m = _BROWSER_ANY_RE.search(text)
        if m and _CHAIN_WORD_RE.search(text[m.end():]):
            m = None
    if not m:
        return text, None
    rest = f"{text[:m.start()]} {text[m.end():]}"
    return re.sub(r"\s{2,}", " ", rest).strip(), m.group(1).lower()


def _browser_key(word: str, apps: dict | None = None) -> str | None:
    """app_registry key for a spoken browser word ('brave' -> 'brave',
    'edge' -> 'msedge'). An uninstalled browser keeps its first candidate
    key so the launcher can say it is missing instead of silently using
    Chrome."""
    spec = _BROWSERS.get((word or "").strip().lower())
    if not spec:
        return None
    apps = _load_app_registry() if apps is None else apps
    return next((k for k in spec[1] if k in apps), spec[1][0])


def _browser_label(key: str) -> str:
    for label, keys in _BROWSERS.values():
        if key in keys:
            return label
    return key


def _remember_site(result: str, name: str, url: str, browser: str) -> str:
    """Record a successful site open so a follow-up 'search X' can reuse it."""
    if not str(result).startswith("[Error]"):
        try:
            from cygnus.brain import conversation_window as cw
            cw.record_site(name or url, url, browser)
        except Exception:
            pass
    return result


def _open_in_chrome(url: str, name: str) -> str:
    """A Chrome tab for url in the user's own, signed-in Chrome: through its
    control port when that is open (no Playwright), else by handing the
    address to chrome.exe (it joins the running Chrome). The JARVIS Chrome
    driver (browser_agent) is the last resort only: when Chrome is already
    open without the port it falls back to a separate, signed-out profile —
    wrong for "open my facebook"."""
    from cygnus.browser import cdp as browser_cdp
    from cygnus.rules import engine as _rules
    label = name or url
    if not _rules.is_web_url(url):
        return f"[Error] {url!r} is not a web address."
    if browser_cdp.is_attached("chrome") and browser_cdp.open_tab("chrome", url):
        return f"Opened {label} in Chrome."
    exe = (_load_app_registry().get("chrome") or {}).get("bin") or ""
    if exe and os.path.isfile(exe):
        subprocess.Popen([exe, url], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return f"Opened {label} in Chrome."
    try:
        return __import__("cygnus.browser.agent", fromlist=["_"]).open_site(url, name=name)
    except Exception as e:
        return f"[Error] Opening {label} in Chrome failed: {type(e).__name__}"


def _open_url_in_browser(url: str, name: str, browser: str | None = None,
                         remember: bool = True) -> str:
    """Open url in the requested browser (an app_registry key).

    Chrome, or no browser, is the unchanged browser_agent debug-Chrome path.
    Any other browser gets its registered bin launched with the URL; a
    missing bin falls back to Chrome and says so. remember=False for search
    result pages, which are not a site the user visited.
    """
    keep = _remember_site if remember else (lambda r, *a: r)
    if not browser or browser in _CHROME_KEYS:
        return keep(_open_in_chrome(url, name), name, url, "chrome")
    label = _browser_label(browser)
    exe = (_load_app_registry().get(browser) or {}).get("bin") or ""
    if not exe or not os.path.isfile(exe):
        return keep(f"{_open_in_chrome(url, name)}\n{label} isn't installed "
                    f"where I expected it, so I used Chrome.", name, url, "chrome")
    try:
        subprocess.Popen([exe, url], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as e:
        return f"[Error] Could not launch {label} for {name or url}: {e}"
    return keep(f"Opened {name or url} in {label}.", name, url, browser)


def _rule_site_url(target: str):
    """(url, owner_key) a user rule aliases to a spoken site name, or None.

    A rule like source_phrase 'visit hd movies' / intent 'it will visit
    hollyhdmovies.cc site' lives on an APP entry, so rules_engine.check never
    sees it for an unresolved site. Phrase-substring match only: the spoken
    target equals or contains the rule's phrase, or names one of the domains
    in its intent. No NLP; no URL in the rule text means no alias. owner_key
    is the app entry holding the rule (e.g. 'brave').
    """
    t = _norm_phrase(target)
    if len(t) < 3:
        return None
    apps = _load_app_registry()
    if not apps:
        return None
    squashed = t.replace(" ", "")
    for owner, entry in apps.items():
        if not isinstance(entry, dict) or entry.get("enabled") is False:
            continue
        for rule in entry.get("compiled_rules") or []:
            phrase = _norm_phrase(rule.get("source_phrase") or "")
            text = f"{rule.get('intent') or ''} {rule.get('source_phrase') or ''}"
            urls = [u.rstrip(".,;:!?)") for u in _RULE_URL_RE.findall(text)]
            if not urls:
                continue
            hit = bool(phrase) and (t == phrase or re.search(
                r"\b" + re.escape(phrase) + r"\b", t))
            if not hit:
                hit = any(re.search(r"\b" + re.escape(squashed) + r"\b", u, re.I)
                          for u in urls)
            if hit:
                return (urls[0] if "://" in urls[0] else "https://" + urls[0],
                        owner)
    return None


def open_site(name: str, url: str = None, _rule_hops: int = 0,
              browser: str | None = None) -> str:
    """Resolve a spoken site name against web_registry.json and open it in
    the JARVIS debug-Chrome. Falls back to treating the input as a URL.

    browser: app_registry key the user asked for ('visit X on Brave').
    Precedence: that > the browser entry owning a matching site rule > Chrome.
    """
    from cygnus.apps import web_registry as wr
    # A spoken sentence ends in a period; it is not part of the name, and
    # neither are "for me" or a trailing "site" ("open GitHub site" failed
    # as the unknown site "GitHub site", 2026-09-12).
    name = _clean_site_name(name) if url is None else strip_target_noise(name)
    if url is None and not _exact_site_key(name):
        alias = _rule_site_url(name)
        if alias:
            alias_url, owner = alias
            if not browser and ((_load_app_registry().get(owner) or {})
                                .get("category") == "browser"):
                browser = owner
            return _open_url_in_browser(alias_url, name, browser)
    key = wr.resolve_site(name) if url is None else None
    if key:
        site = wr.get_site(key)
        # Phase 3 runtime enforcement: a site's compiled_rules can deny the
        # open outright or redirect it to another registered site.
        try:
            from cygnus.rules import engine as _rules
            gate = _rules.check(key, {"action": "open", "entry": site})
        except Exception:
            gate = {"action": "allow"}
        if gate.get("action") == "deny":
            return f"[Error] Launch blocked by rule: {gate.get('reason', '')}"
        if gate.get("action") == "redirect" and gate.get("target"):
            if _rule_hops >= 1:
                return (f"[Error] Launch blocked by rule: redirect loop while "
                        f"opening {key}.")
            return open_site(gate["target"], _rule_hops=_rule_hops + 1,
                             browser=browser)
        opened = _open_url_in_browser(site["url"], key, browser)
        if gate.get("warning"):
            opened = f"{opened}\n{gate['warning']}"
        return opened
    raw = (url or name or "").strip().rstrip(".!?").strip()
    if "://" not in raw and _BARE_HOST_RE.fullmatch(raw):
        raw = "https://" + raw
    if "://" in raw:
        return _open_url_in_browser(raw, name, browser)
    return (f"[Error] No registered site matches '{name}'. "
            "Say 'add site <name> <url>' to register it, or 'rescan my sites'.")


def rescan_sites() -> str:
    """Full incremental rescan of browser history into web_registry.json."""
    from cygnus.apps import web_registry as wr
    reg = wr.scan_sites()
    wr.build_index()
    n = len(reg.get("sites", {}))
    return f"Rescanned sites — {n} known now (last scan {reg.get('last_scan')})."


# ---- Phase 13: session recall (FTS5 over JARVIS's own transcripts) ---------

def search_sessions(query: str, limit: int = 10) -> str:
    """Full-text search over past JARVIS conversations."""
    from cygnus.memory import session_index as si
    si.rebuild()  # cheap incremental; picks up any lines written by other processes
    hits = si.search_sessions(query, int(limit or 10))
    if not hits:
        return f"No past conversation turns matched '{query}'."
    return (f"Found {len(hits)} past turn(s) matching '{query}':\n"
            + si.format_hits(hits))


# Moved to phrases.py (restructure phase 5); re-exported so
# `tools.<name>` keeps working for callers and tests.
from cygnus.tools.phrases import (  # noqa: E402,F401
    _AI_HISTORY_SITES,
    _AI_TYPED_SITES,
    _clean_site_name,
    _CORRECTION_START,
    _DEICTIC_TAIL,
    _drop_quoted_spans,
    _drop_relayed_payload,
    _FILLER_START,
    _LEAD_HISTORY,
    _QUESTION_START,
    _QUOTE_CARRIER_RE,
    _QUOTED_SPAN_RE,
    _REFERENT_RE,
    _RELAY_FRAME_RE,
    _RESEARCH_TAIL_RE,
    _SCHEME_BEFORE_COLON_RE,
    _SEARCH_FRAMING,
    _SEARCH_NAMED,
    _SEARCH_START,
    _SITE_SUFFIX_RE,
    _TAIL_TARGET,
    _TARGET_LEAD_RE,
    _TARGET_TAIL_RE,
    _TRANSPORT_REFERENT_RE,
    _WEB_LEAD_RE,
    is_referent,
    is_transport_referent,
    outer_request,
    strip_correction_prefix,
    strip_fillers,
    strip_target_noise,
)


def _exact_site_key(cand: str):
    """Exact-only registry lookup (name or alias). NEVER fuzzy here: a search
    tail like 'manila' must stay part of the query, not become a website."""
    from cygnus.apps import web_registry as wr
    wr.ensure_index()
    q = cand.strip().lower().rstrip(".!?")
    return wr._index["exact"].get(q)


# A search verb with no query behind it. Same guard shape as
# close_application's bare-pronoun reject: name the missing thing and give an
# example, never fire the tool. Without this, "search" opened a Google tab for
# the empty string and reported "Opened search: ".
_BARE_SEARCH_OBJECT = {"", "it", "this", "that", "them", "something",
                       "anything", "stuff", "things", "for it", "for that",
                       "for this", "for something", "for anything"}
_SEARCH_CLARIFY = ("[Error] What should I search for, sir? Name the query, "
                   "e.g. 'search Bohemian Rhapsody'.")


def parse_search_command(text: str):
    """Parse a spoken search command.

    Returns dict: {query, target, browser, is_history_search, corrected,
    clarify?} or None when this isn't a search command. Target may be None
    (= Google); browser is an app_registry key when one was spoken.
    `clarify` is set when the verb arrived with no query — execute_search
    returns it verbatim instead of searching for nothing.
    """
    raw = strip_fillers(text)
    # "search movies, Jarvis." — the name spoken last is not part of the query.
    raw = _TRAILING_VOCATIVE_RE.sub("", raw).strip() or raw
    # A correction marker may precede the verb ("I mean search ...").
    m_corr = _CORRECTION_START.match(raw)
    if m_corr:
        raw = strip_fillers(re.sub(_CORRECTION_START, "", raw).strip())
        corrected = True
    else:
        corrected = False
    m = _SEARCH_START.match(raw)
    if not m:
        return None
    body = raw[m.end():].strip()

    # "search the web for X": the whole web, never the last site opened.
    # Research phrasing ("... and summarize them") isn't a search tab at all:
    # let the brain hand it to the executor.
    web = _WEB_LEAD_RE.match(body)
    if web:
        body = body[web.end():].strip()
        if _RESEARCH_TAIL_RE.search(body):
            return None

    # correction marker: replace previous turn's query rather than append.
    # The marker may sit before the verb ("I mean search ...") or after it
    # (caller strips the verb first) — handle both.
    if not corrected:
        corrected = bool(_CORRECTION_START.match(body))
        if corrected:
            body = re.sub(_CORRECTION_START, "", body).strip()
            m2 = _SEARCH_START.match(body)
            if m2:
                body = body[m2.end():].strip()

    # leading "history" phrasing: "search in history for irimsv report"
    lead_history = bool(_LEAD_HISTORY.match(body))
    if lead_history:
        body = re.sub(_LEAD_HISTORY, "", body).strip()
        if not body:
            return {"query": "", "target": "chatgpt", "browser": None,
                    "is_history_search": True, "corrected": corrected,
                    "clarify": _SEARCH_CLARIFY}

    # Query cleaning: "me a movie on Brave named The Odyssey" -> query
    # "The Odyssey", browser brave. A question keeps its browser words.
    body = _SEARCH_FRAMING.sub("", body, count=1).strip()
    browser = None
    if not _QUESTION_START.match(body):
        body, bword = _split_browser(body)
        browser = _browser_key(bword) if bword else None
    body = _SEARCH_NAMED.sub("", body, count=1).strip()
    # "search lo-fi music there": "there" points at the last site (the
    # no-target path already uses it); it is not part of the query.
    body = _DEICTIC_TAIL.sub("", body).strip() or body

    # tail qualifier: "... in <target>" — candidate capped at a few words,
    # resolved EXACTLY against AI names then the site registry.
    target = None
    is_hist = lead_history
    tail = _TAIL_TARGET.search(body)
    if tail:
        cand = tail.group(1).strip().lower().rstrip(".!?")
        hit = (_AI_HISTORY_SITES.get(cand) or _AI_HISTORY_SITES.get(cand + " gpt"))
        typed = None
        if not hit:
            # try the last 1-3 words of the candidate as an AI/site name
            words = cand.split()
            for n in (3, 2, 1):
                frag = " ".join(words[-n:]) if len(words) >= n else cand
                if frag in _AI_HISTORY_SITES:
                    hit = _AI_HISTORY_SITES[frag]
                    break
                if frag in _AI_TYPED_SITES:
                    typed = _AI_TYPED_SITES[frag]
                    break
                key = _exact_site_key(frag)
                if key:
                    typed = key
                    break
        if hit:
            target = hit
            is_hist = True
            body = body[:tail.start()].strip().rstrip(",.")
        elif typed:
            target = typed
            body = body[:tail.start()].strip().rstrip(",.")

    out = {"query": body, "target": target, "browser": browser,
           "is_history_search": is_hist,
           "corrected": corrected, "web": bool(web)}
    if body.strip().lower().rstrip("?.!, ") in _BARE_SEARCH_OBJECT:
        out["clarify"] = _SEARCH_CLARIFY
    return out


# Sites whose own search page takes the query in the address. "Search X
# there" goes straight to it instead of a Google site: query.
_SITE_SEARCH = {
    "youtube.com": "https://www.youtube.com/results?search_query={query}",
    "m.youtube.com": "https://www.youtube.com/results?search_query={query}",
    "music.youtube.com": "https://music.youtube.com/search?q={query}",
}


def _site_search_url(host: str) -> str | None:
    """A search address for `host` with {query} in it: a known site, else one
    a rule setup verified (site_probe's cache). None when unknown."""
    template = _SITE_SEARCH.get(host)
    if not template:
        try:
            from cygnus.browser import site_probe
            template = site_probe._read_cache().get(host)
        except Exception:
            template = None
    return template if isinstance(template, str) and "{query}" in template else None


def execute_search(parsed: dict) -> str:
    """Run a parsed search command against the right backend."""
    from cygnus.apps import web_registry as wr
    if parsed.get("clarify"):
        return parsed["clarify"]
    q, target = parsed["query"], parsed.get("target")

    if parsed.get("is_history_search"):
        # ChatGPT has real conversation-history search via browser_agent
        return __import__("cygnus.browser.agent", fromlist=["_"]).search_chatgpt_history(q)

    if target:
        site = wr.get_site(target)
        url = site["url"] if site else f"https://{target}.com"
        # AI web chats get the query typed into the composer; plain sites just open
        if target in _AI_TYPED_SITES.values():
            return __import__("cygnus.browser.agent", fromlist=["_"]).ask_web_ai(prompt=q, site=target)
        result = __import__("cygnus.browser.agent", fromlist=["_"]).open_site(url, name=target)
        return f"{result} — search for '{q}' manually there; I can't search inside that site yet."

    # No site named: reuse the last site opened this session, if any, as a
    # Google site: query in its browser (a spoken browser wins). Nothing
    # remembered and no browser spoken -> the unchanged default search.
    browser = parsed.get("browser")
    try:
        from cygnus.brain import conversation_window as cw
        site = None if parsed.get("web") else cw.last_site()
    except Exception:
        site = None
    if not site and not browser:
        return search_web(q)
    browser = browser or site.get("browser") or None
    host = ""
    if site:
        host = urllib.parse.urlsplit(site["url"]).netloc.lower()
        host = host[4:] if host.startswith("www.") else host
    template = _site_search_url(host) if host else None
    if template:
        url = template.replace("{query}", urllib.parse.quote_plus(q))
    else:
        terms = f"site:{host} {q}" if host else q
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(terms)
    opened = _open_url_in_browser(url, f"search: {q}", browser, remember=False)
    if opened.startswith("[Error]"):
        return opened
    note = opened.split("\n", 1)[1] if "\n" in opened else ""
    used = "Chrome" if note else _browser_label(browser or "chrome")
    if template:
        msg = f"Searching {site.get('key') or host} for {q} in {used}."
    elif host:
        msg = (f"Searching {q} (on {site.get('key') or host}, {used}) — Google "
               f"results limited to {host}; I can't search inside the site itself.")
    else:
        msg = f"Searching {q} on Google in {used}."
    return f"{msg}\n{note}" if note else msg


# ---- Apps-panel action rules ----------------------------------------------
# 'search a movie in brave' -> 'search hollymoviehd.cc for the name' is
# compiled by rules_ai into an action; rules_engine.match_action finds it in
# the spoken command (no model call) and run_rule_action carries it out. A
# missing name is asked for once, and the next turn answers it.

_PENDING_RULE_SLOT: dict = {}
_RULE_SLOT_TTL = 120  # seconds the "Which movie, sir?" question stays open
_RULE_CANCEL_RE = re.compile(
    r"^(?:no|nope|cancel|never\s*mind|forget\s+it|stop)\b[\s.!]*$", re.I)
_NEW_COMMAND_RE = re.compile(
    r"^(?:search|find|look\s*up|open|launch|visit|go\s+to|play|close|stop|pause|"
    r"what|who|how|why|when|where|which|can|could|is|are|do|does|tell|remember|"
    r"rescan|volume|turn|set|delete|remove|create|make|send|write|download|install|"
    r"email|message)\b", re.I)
_SLOT_LEAD_RE = re.compile(r"^(?:it'?s|it\s+is|the\s+one\s+called|called|named)\s+", re.I)
_TRAILING_VOCATIVE_RE = re.compile(r"[,\s]+(?:jarvis|cygnus|sir)[.!?,\s]*$", re.I)


def run_rule_action(rule: dict, owner: str, action: dict, slot: str = "",
                    entry: dict | None = None) -> str:
    """Carry out one runnable rule (search_site / open_site)."""
    import time as _time
    from cygnus.rules import engine as _rules
    if action.get("type") == "steps":
        from cygnus.rules import steps as rules_steps
        return rules_steps.start(rule, owner, action, slot)
    t0 = _time.time()
    slot = (slot or "").strip().strip(".,!?")
    site = action.get("site") or _rules.bare_host(action.get("url") or "") or owner
    browser = action.get("browser") or None
    if action.get("type") == "search_site" and not slot:
        _PENDING_RULE_SLOT.clear()
        _PENDING_RULE_SLOT.update(rule=rule, owner=owner, action=action, ts=_time.time())
        # slot "movie name" -> "Which movie, sir?"
        label = re.sub(r"\s+name$", "", action.get("slot") or "") or "one"
        question = f"Which {label}, sir?"
        from cygnus.brain import dialogue_state
        dialogue_state.ask("slot", question, {"rule": rule.get("rule_id")}, owner="tools")
        return question
    url = _rules.build_action_url(action, slot)
    if not url:
        return f"[Error] The rule '{rule.get('source_phrase', '')}' has no website to open."
    searching = (action.get("type") == "search_site"
                 and "{query}" in (action.get("search_url") or ""))
    opened = _open_url_in_browser(url, f"{site} search: {slot}" if searching else site,
                                  browser, remember=not searching)
    if opened.startswith("[Error]"):
        out = opened
    else:
        note = opened.split("\n", 1)[1] if "\n" in opened else ""
        used = "Chrome" if note else _browser_label(browser or "chrome")
        if searching:
            msg = f"Searching {site} for {slot} in {used}."
            from cygnus.rules import steps as rules_steps
            rules_steps.remember("results", {"rule": rule, "owner": owner, "action": action,
                                             "slot": slot, "entry": entry}, url=url)
            msg += _learn_preference(action, site, browser)
        elif slot:
            msg = (f"Opened {site} in {used} — look for {slot} there; this rule "
                   f"has no search address yet.")
        else:
            msg = f"Opened {site} in {used}."
        out = f"{msg}\n{note}" if note else msg
    try:
        from cygnus import audit
        audit.log_call("rules.action", {"rule": rule.get("rule_id"), "owner": owner,
                                        "slot": slot, "url": url},
                       _time.time() - t0, out)
    except Exception:
        pass
    return out


def _learn_preference(action: dict, site: str, browser: str | None) -> str:
    """Count this search toward a learned default; after a streak, ask once
    ("Make that the default?") and hold the question for the next reply."""
    try:
        from cygnus.brain import dialogue_state
        from cygnus.memory import preferences
        topic = re.sub(r"\s+name$", "", action.get("slot") or "site") + " search"
        question = preferences.observe(topic, {"site": site, "browser": browser})
        if question:
            dialogue_state.ask("preference", question, {"topic": topic}, owner="tools")
            return " " + question
    except Exception:
        pass
    return ""


# A reply that is ONLY a yes or no answers "Make that the default?" (read by
# consent.py); anything longer ("yes, search movie Godzilla") is a new command
# and is routed as one. "Make it the default" says yes in so many words.
_MAKE_DEFAULT_RE = re.compile(r"^make it (?:the )?default[\s.!]*$", re.I)


_LEAD_ACK_RE = re.compile(r"^(?:yes|yeah|yep|ok(?:ay)?|sure)[,.!]?\s+(?=\S)", re.I)


def _answer_preference(text: str):
    """Yes/no to our "Make that the default?" question, else None."""
    from cygnus.brain import dialogue_state
    from cygnus.memory import preferences
    asked = dialogue_state.pending("preference")
    if not asked:
        return None
    from cygnus.brain import consent
    topic = asked["payload"].get("topic")
    reply = consent.read(text, soft=True, extra_yes=_MAKE_DEFAULT_RE)
    if reply.kind == "yes":
        saved = preferences.confirm(topic)
        dialogue_state.answered()
        return f"Saved. {topic.capitalize()} now defaults to {preferences._describe(saved)}." \
            if saved else None
    if reply.kind == "no":
        preferences.decline(topic)
        dialogue_state.answered()
        return "Okay, I won't ask about that again."
    return None


def try_rule_action(text: str, semantic: bool = True):
    """Answer a spoken command from the Apps-panel action rules, else None.

    Also answers our own 'Which movie, sir?' follow-up: while that question is
    open, a reply that is not a new command is the missing name. semantic=False
    skips the last tier, the model call that reads a rule by meaning: the turn
    passes it when an instant answer is waiting (time, maths, a greeting).
    """
    import time as _time
    # "I mean search movie X" / "no, search movie X" is the same command.
    t = strip_fillers(strip_correction_prefix(strip_fillers(text or "")))
    t = _TRAILING_VOCATIVE_RE.sub("", t).strip()
    if not t:
        return None
    try:
        answer = _answer_preference(t)
        if answer is not None:
            return answer
    except Exception:
        pass
    # "yes, search movie Godzilla": the leading okay isn't part of the command.
    t = _LEAD_ACK_RE.sub("", t).strip() or t
    pend = dict(_PENDING_RULE_SLOT)
    _PENDING_RULE_SLOT.clear()
    try:
        from cygnus.rules import engine as _rules
        from cygnus.rules import steps as rules_steps
        # Our own open questions first: "Did you mean your gym time rule?"
        # and "Change the rule to ...?" take a yes/no before anything else.
        answer = _answer_rule_guess(t)
        if answer is not None:
            return answer
        answer = _answer_rule_fix(t)
        if answer is not None:
            return answer
        # "That didn't work": the last rule that ran gets corrected.
        fix = _start_rule_fix(t)
        if fix is not None:
            return fix
        m = _rules.match_action(t)
        # A multi-step rule waiting on this user ("Which one?", "Press play?")
        # gets the reply first, unless the reply is itself a rule command.
        if rules_steps.active() and not m:
            out = rules_steps.resume(t, looks_new=bool(_NEW_COMMAND_RE.match(t)))
            if out is not None:
                return out
        if m:
            return _run_matched_rule(m, t)
        # "play it" / "open number 2" right after a rule showed results.
        if rules_steps.is_follow_up(t):
            return rules_steps.follow_up(t)
        if pend and _time.time() - pend.get("ts", 0) <= _RULE_SLOT_TTL:
            if _RULE_CANCEL_RE.match(t):
                return "Okay, cancelled."
            # "search The Social Network" answers "Which movie?" for a search
            # rule even though it starts like a new command.
            answer = _rules.answer_slot(t, pend["rule"])
            if answer or not _NEW_COMMAND_RE.match(t):
                slot = answer or _SLOT_LEAD_RE.sub("", t).strip(" .!?,")
                return run_rule_action(pend["rule"], pend["owner"], pend["action"], slot)
        # The fast tier missed: read the meaning. "It's gym time" is the
        # "gym time" rule; "sum up this column" is "total it".
        if not semantic:
            return None
        from cygnus.rules import match as rules_match
        sm = rules_match.semantic(t)
        if sm and sm["confidence"] >= rules_match.SURE:
            rules_match.learn_trigger(sm["owner"], sm["rule"].get("rule_id"), t)
            return _run_matched_rule(sm, t)
        if sm and sm["confidence"] >= rules_match.ASK:
            from cygnus.brain import dialogue_state
            question = f"Did you mean {rules_match.describe(sm)}?"
            _RULE_GUESS.clear()
            _RULE_GUESS.update(match=sm, said=t, ts=_time.time())
            dialogue_state.ask("rule_guess", question, {"rule": sm["rule"].get("rule_id")},
                               owner="tools")
            return question
    except Exception as e:
        print(f"[JARVIS] action-rule check failed: {e}")
    return None


# The semantic tier's "Did you mean ...?" and the last rule that ran, for
# "that didn't work" (rules_match / RULES_INTENT_PLAN.md phases 3 and 4).
_RULE_GUESS = {}
_LAST_RULE_RUN = {}
_RULE_GUESS_TTL = 120
_LAST_RUN_TTL = 600
_DIDNT_WORK_RE = re.compile(
    r"^(?:that|it|this|no,?\s*that|no,?\s*it)?\s*(?:didn'?t|did\s+not|doesn'?t|does\s+not|"
    r"is\s+not|isn'?t|wasn'?t|was\s+not)\s+(?:work|right|it|what\s+i\s+(?:wanted|meant|asked)|"
    r"correct)\b|^(?:wrong|not\s+(?:that|right|what\s+i\s+(?:wanted|meant)))\b", re.I)
_EXPECT_HINTS = {}


def _run_matched_rule(m, said):
    """Run a matched rule (fast or semantic tier): its time condition first,
    then the run, then a first-time hint on what the user should see."""
    import time as _time
    from cygnus.rules import engine as _rules
    from cygnus.rules import steps as rules_steps
    rule = m["rule"]
    if not _rules.condition_holds(rule):
        from cygnus.rules import ai as rules_ai
        when = rules_ai.describe_condition(rule.get("condition"))
        return (f'Your rule "{rule.get("source_phrase") or rule.get("rule_id")}" only '
                f"applies {when}, sir, so I left it.")
    if m["action"].get("type") == "steps":
        out = rules_steps.start(rule, m["owner"], m["action"], m["slot"], m.get("entry"))
    else:
        out = run_rule_action(rule, m["owner"], m["action"], m["slot"], m.get("entry"))
    _LAST_RULE_RUN.clear()
    _LAST_RULE_RUN.update(rule=rule, owner=m["owner"], said=said, ts=_time.time())
    if out and not out.startswith("[Error]") and rule.get("expects") \
            and not rules_steps.active():
        rid = f'{m["owner"]}/{rule.get("rule_id")}'
        n = _EXPECT_HINTS.get(rid, 0)
        if n < 2:
            _EXPECT_HINTS[rid] = n + 1
            out = (f"{out.rstrip()} You should see: {rule['expects']}. "
                   f"If not, say \"that didn't work\".")
    return out


def _answer_rule_guess(text):
    """Yes/no to "Did you mean your <rule> rule?", else None."""
    import time as _time
    from cygnus.brain import dialogue_state
    asked = dialogue_state.pending("rule_guess")
    guess = dict(_RULE_GUESS)
    if not asked or not guess or _time.time() - guess.get("ts", 0) > _RULE_GUESS_TTL:
        return None
    from cygnus.brain import consent
    reply = consent.read(text, soft=True)
    if reply.kind == "yes":
        _RULE_GUESS.clear()
        dialogue_state.answered()
        from cygnus.rules import match as rules_match
        sm = guess["match"]
        rules_match.learn_trigger(sm["owner"], sm["rule"].get("rule_id"), guess["said"])
        return _run_matched_rule(sm, guess["said"])
    if reply.kind == "no":
        _RULE_GUESS.clear()
        dialogue_state.answered()
        return "Okay, I'll leave it."
    return None


def _start_rule_fix(text):
    """"That didn't work" after a rule ran: ask what should have happened, or
    take the correction in the same breath and propose a new version."""
    import time as _time
    last = dict(_LAST_RULE_RUN)
    if not last or _time.time() - last.get("ts", 0) > _LAST_RUN_TTL:
        return None
    m = _DIDNT_WORK_RE.match(text or "")
    if not m:
        return None
    rest = (text or "")[m.end():].strip(" .,;:!?-")
    rest = re.sub(r"^(?:it\s+should|i\s+wanted\s+it\s+to|i\s+meant|instead)\s*", "", rest, flags=re.I).strip()
    rule = last["rule"]
    from cygnus.brain import dialogue_state
    if not rest:
        _RULE_FIX.clear()
        _RULE_FIX.update(owner=last["owner"], rule=rule, ts=_time.time(), stage="ask")
        question = (f'Sorry, sir. For "{rule.get("source_phrase") or rule.get("rule_id")}", '
                    f"what should have happened?")
        dialogue_state.ask("rule_fix", question, {"rule": rule.get("rule_id")}, owner="tools")
        return question
    return _propose_rule_fix(last["owner"], rule, rest)


_RULE_FIX = {}


def _propose_rule_fix(owner, rule, feedback):
    """Recompile the rule with the user's correction and hold the proposal
    for a yes (save) or another correction."""
    import time as _time
    from cygnus.brain import dialogue_state
    from cygnus.rules import voice as rules_voice
    step = rules_voice.begin_fix(owner, rule, feedback)
    if step.get("status") != "proposal":
        _RULE_FIX.clear()
        return step.get("question") or step.get("error") or "I couldn't rework that rule, sir."
    _RULE_FIX.clear()
    _RULE_FIX.update(owner=owner, rule=rule, ts=_time.time(), stage="proposal")
    question = (f"Here's a new version: {step.get('summary') or ''} "
                f"Say yes to save it, or tell me what to change.").strip()
    dialogue_state.ask("rule_fix", question, {"rule": rule.get("rule_id")}, owner="tools")
    return question


def _answer_rule_fix(text):
    """The reply to "what should have happened?" or to the reworked proposal."""
    import time as _time
    from cygnus.brain import dialogue_state
    asked = dialogue_state.pending("rule_fix")
    fix = dict(_RULE_FIX)
    if not asked or not fix or _time.time() - fix.get("ts", 0) > _RULE_GUESS_TTL:
        return None
    from cygnus.brain import consent
    reply = consent.read(text, soft=True)
    if reply.kind == "no" or _RULE_CANCEL_RE.match(text or ""):
        _RULE_FIX.clear()
        dialogue_state.answered()
        from cygnus.rules import voice as rules_voice
        rules_voice.cancel_setup(fix["owner"])
        return "Okay, the rule stays as it is."
    if fix.get("stage") == "proposal" and reply.kind == "yes":
        from cygnus.rules import voice as rules_voice
        step = rules_voice.handle_clarification(fix["owner"], "", "yes")
        _RULE_FIX.clear()
        dialogue_state.answered()
        if step.get("status") == "committed":
            return "Saved. The rule now does that."
        return step.get("question") or step.get("error") or "I couldn't save it, sir."
    if _NEW_COMMAND_RE.match(text or "") and fix.get("stage") == "ask":
        # A new command, not an answer: drop the question.
        _RULE_FIX.clear()
        dialogue_state.answered()
        return None
    dialogue_state.answered()
    return _propose_rule_fix(fix["owner"], fix["rule"], text)


def _is_app_key(name: str) -> bool:
    return name in (_load_app_registry() or {})




def resolve_open_target(text: str, force: str | None = None,
                        browser: str | None = None):
    """Dual-registry lookup for 'open X' style commands.

    Returns ('site', key) | ('app', name) | ('clarify', candidates) |
    (None, text) when nothing matches — caller falls back to existing behavior.
    force: 'app' | 'site' honours explicit 'open the spotify app' /
    'facebook site' phrasing. browser: a spoken 'on Brave' makes X a site,
    so an app hit never wins or asks.
    """
    t = strip_target_noise(text).lower()
    # strip filler words
    for w in ("please", "jarvis", "cygnus", "the ", "my ", "up ", "now "):
        if t.startswith(w):
            t = t[len(w):]
    for suffix, forced in ((" app", "app"), (" application", "app"),
                           (" program", "app"), (" site", "site"),
                           (" website", "site"), (" in browser", "site")):
        # "ollama app" is itself an app's name: the suffix is not a hint.
        if t.endswith(suffix) and not _is_app_key(t):
            t = t[: -len(suffix)].strip()
            force = force or forced

    # A user rule that aliases this phrase to a URL ("visit hd movies" ->
    # hollyhdmovies.cc) outranks the fuzzy registry match; open_site() takes
    # the same alias when handed the name.
    if force != "app" and _rule_site_url(t):
        return ("site", t)

    from cygnus.apps import web_registry as wr
    site_key = wr.resolve_site(t)
    if browser and force != "app":
        return ("site", site_key) if site_key else (None, text)

    app_hit = False
    try:
        with open(paths.APP_REGISTRY, encoding="utf-8") as f:
            apps = json.load(f).get("apps", {})
        # A name open_application knows by alias ("vs code" -> code) is an
        # app: "open VS Code" otherwise fell through to the coding specialist
        # because of the word "code" (2026-09-12).
        app_hit = t in apps or t in APP_ALIASES
        if not app_hit:
            # fuzzy on app names too (spotify vs spotifly etc.)
            close = difflib.get_close_matches(t, list(apps.keys()), n=1, cutoff=0.85)
            app_hit = bool(close)
    except Exception:
        pass

    if force == "site" and site_key:
        return ("site", site_key)
    if force == "app":
        return ("app", t) if app_hit else (None, text)

    if site_key and app_hit:
        # An exact name on one side beats a fuzzy one on the other: "open
        # grok" asked app-or-site because "grok" is close to the app ngrok.
        site_exact = site_key == t or t in ((wr.get_site(site_key) or {}).get("aliases") or [])
        app_exact = t in apps
        if site_exact != app_exact:
            return ("site", site_key) if site_exact else ("app", t)
        return ("clarify", {"name": t, "site": site_key})
    if site_key:
        return ("site", site_key)
    if app_hit:
        return ("app", t)
    # Every lookup has missed. When what is left is a referent and not a name,
    # the request is open-shaped with nothing to open: ask which one instead of
    # letting the caller's fallback open an app called "it". The payload has no
    # "name" - that key means the app/site collision above, and callers tell the
    # two apart by it. The match is on the normalised `t` ("the usual" ->
    # "usual"), but what comes back is the target AS SPOKEN: the question put to
    # the user quotes it, and "which usual, sir" is not what they said.
    if _REFERENT_RE.match(t):
        return ("clarify", {"referent": (text or "").strip()})
    return (None, text)


# Agents JARVIS can hand a job to when it cannot do it itself. Resolved through
# the PATH entry, not a hardcoded location, so a reinstall does not break this.
DELEGATE_AGENTS = {
    "claude": ["claude", "-p"],
    "hermes": ["hermes", "-p"],
}
# A delegated agent runs a whole session of its own. Long, but bounded: a voice
# turn that never returns is worse than one that reports a timeout.
DELEGATE_TIMEOUT = int(os.getenv("JARVIS_DELEGATE_TIMEOUT", "300"))


# An npm shim (.cmd) runs through cmd.exe, which ends an argument at its first
# line break and reads & | " inside it as its own syntax: a task sent to
# OpenCode arrived as its brief's first line only (2026-09-13), and prompt text
# could run commands. Agents start from the shim's real target instead.
_SHIM_TARGET_RE = re.compile(r'"%dp0%\\([^"%]+\.(?:exe|js))"', re.I)


def _direct_launcher(bin_name: str) -> list | None:
    """The argv prefix that runs `bin_name` without cmd.exe, or None when it
    isn't installed or is a shim whose target can't be found."""
    path = shutil.which(bin_name) if bin_name else None
    if not path or not path.lower().endswith((".cmd", ".bat")):
        return [path] if path else None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            targets = _SHIM_TARGET_RE.findall(f.read())
    except OSError:
        return None
    target = os.path.join(os.path.dirname(path), targets[-1]) if targets else ""
    if not os.path.isfile(target):
        return None
    if target.lower().endswith(".exe"):
        return [target]
    node = os.path.join(os.path.dirname(path), "node.exe")
    node = node if os.path.isfile(node) else shutil.which("node")
    return [node, target] if node else None


def delegate_task(task: str, agent: str = "claude") -> str:
    """Hand a task to a CLI coding agent and return what it reports back.

    For work beyond JARVIS's own tools — multi-file edits, debugging, anything
    needing a full agent loop. It is deliberately NOT the fast path: the agent
    runs a complete session, so this costs far more than a normal reply.
    """
    key = str(agent or "claude").strip().lower()
    if key not in DELEGATE_AGENTS:
        return (f"[Error] I can delegate to {' or '.join(sorted(DELEGATE_AGENTS))}, "
                f"not '{agent}'.")
    if not str(task).strip():
        return "[Error] No task given to delegate."

    launch = _direct_launcher(DELEGATE_AGENTS[key][0])
    if not launch:
        return (f"[Error] The {key} CLI is not on PATH, so I cannot delegate to it.")

    cmd = launch + DELEGATE_AGENTS[key][1:] + [str(task)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=DELEGATE_TIMEOUT, encoding="utf-8",
                              errors="replace")
    except subprocess.TimeoutExpired:
        return (f"[Error] {key} did not finish within {DELEGATE_TIMEOUT}s. "
                "The task may be too large to delegate from a voice turn.")
    except Exception as e:
        return f"[Error] Could not run {key}: {type(e).__name__}: {e}"

    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        return f"[Error] {key} exited with code {proc.returncode} and said nothing."
    if not out:
        return f"{key} finished but returned no output."
    # Spoken aloud, so hand back a readable slice rather than a wall of text.
    return f"{key} reports: {out[:1500]}"


# ── Hermes delegation bridge (full harness, approval-gated) ────────────────
# JARVIS is the voice peripheral (mic in, speaker out); Hermes is the EXECUTOR
# with the full machine toolkit (terminal, file, browser, code_execution,
# delegation/subagents, cron, skills, memory, computer_use). JARVIS hands a
# self-contained task to the local Hermes agent, which runs as a SEPARATE
# process with the FULL toolset, so it can actually get work done on this box.
#
# SAFETY MODEL (scope A, approved by user 2026-08-22):
#   * READ / compute / safe-local actions run immediately — "fully utilize".
#   * DESTRUCTIVE actions (delete/overwrite/install/git push/kill/sudo/...) are
#     NOT auto-run. The first call returns a NEEDS_CONFIRM notice describing the
#     action; only a second call with confirm=true executes it. This is the
#     user's "I approve destructive acts" gate, enforced structurally — Hermes
#     never sees the destructive task until confirmation is given.
#
# This is intentionally distinct from the broken delegate_task(agent="hermes"),
# which used `hermes -p` (not a real flag) and had no confirm gate.
_HERMES_BIN = shutil.which("hermes") or "hermes"

# Full toolset: Hermes may use everything. (Keep this as the live default; the
# confirm-regex below is what restrains destructive work, not tool stripping.)
_HERMES_FULL_TOOLSETS = (
    "web,browser,terminal,file,code_execution,vision,video,image_gen,"
    "video_gen,bfl,x_search,tts,skills,todo,memory,session_search,clarify,"
    "delegation,cronjob,computer_use"
)

# Phase 1: Allowlist foundation — flip from destructive blocklist to safe allowlist.
# Previously: gate only if destructive verb matched. Now: gate UNLESS safe.
# Safe = read/compute + deterministic local actions that never mutate state.
# Everything state-changing (create/write/delete/install/deploy) requires confirm.
# This is load-bearing before concurrency + real file access (Phases 2 + 7).
_SAFE_ALLOWLIST_RE = re.compile(
    r"""(?ix)
      ^\s*(what|who|how|why|when|where|which|explain|tell|describe|summarize|define)\b
    | \b(read|list|show|search|find|lookup|lookup|query|get|check|status|help)\b
    | \b(open|launch|play|stop|visit|browse|navigate)\b.{0,40}\b(app|music|song|file|folder|site|project|page|url|link|web)?\b
    | \b(go|take|send|point)\b.{0,15}\b(me\s+)?(to|me)\b.{0,20}\b(site|page|url|link|web|app)\b
    | \b(remember|recall|history|previous)\b
    | \b(rescan|refresh|scan)\b.{0,20}\b(apps?|sites?)\b
    | \b(time|date|weather)\b
    | ^\s*(hello|hi|hey|thanks|thank you)\b
    """
)
# File-mutating verbs — even if allowlist matches, these need confirm (Phase 7).
# 2026-09-11: "empty my recycle bin" matched none of these, read as chat, and
# ran unconfirmed. Emptying, clearing, resetting and the like are mutations.
_MUTATING_RE = re.compile(
    r"""(?ix)
      \b(create|make|build|generate|write|save|update|overwrite|clobber|replace|delete|remove|erase|wipe|purge|trash|format|drop|unlink|install|pip\s+install|npm\s+(i|install)|git\s+(push|reset|clean|checkout|rm)|chmod|chown|mkfs|sudo|deploy|migrate)\b
    | \b(empty|emptied|clear|clean(\s*up)?|reset|flush|discard|uninstall|restore|revoke|rename|move|shred|nuke|kill|terminate|reboot|restart|shut\s*down|log\s*(out|off)|sign\s*out|disable)\b
    | \bget\s+rid\s+of\b
    """
)
# Verbs that change files when given as an instruction. Kept apart from
# _MUTATING_RE so a question ("how do I fix my wifi?") still reads as chat.
_CHANGE_RE = re.compile(
    r"\b(fix|repair|patch|edit|modify|refactor|rewrite|redesign|restyle|tweak|adjust|"
    r"improve|change|implement|resize|reposition|rearrange)\b", re.I)
# Politeness and request framing before the real instruction: "please",
# "can you", "I need you to", "go ahead and".
_REQUEST_LEAD_RE = re.compile(
    r"^(?:(?:please|jarvis|cygnus|sir|hey|ok(?:ay)?|now|so|just|go\s+ahead\s+and|"
    r"(?:can|could|would|will)\s+you|i\s+(?:want|need)\s+you\s+to)\b[,\s]*)+", re.I)
# How chat starts: a pronoun, question word, auxiliary, greeting or a reply.
# A task starting with anything else reads as an instruction ("empty my
# recycle bin", "nuke my downloads"), whatever its verb.
_CHAT_START = frozenset("""
    i i'm im i've i'd i'll my me mine we we're our us you you're your it it's its
    that that's this these those the a an there here what what's who who's whose
    how how's why when where which is are am was were do does did can could would
    will should shall may might have has had hello hi hey thanks thank good yes
    yeah yep no nope ok okay sure cool nice great awesome wow lol haha hmm
""".split())


def _reads_as_command(task: str) -> bool:
    """True when the task is an instruction rather than chat: it is framed
    as a request ("can you ..."), or its first real word isn't how chat
    starts. Used so an unlisted verb can't slip past the confirm gate."""
    t = (task or "").strip()
    lead = _REQUEST_LEAD_RE.match(t)
    if lead and re.search(r"\byou\b", lead.group(0), re.I):
        return True
    rest = t[lead.end():] if lead else t
    words = re.findall(r"[a-z']+", rest.lower())
    return bool(words) and words[0] not in _CHAT_START

# Legacy destructive regex kept for audit classification (not gating).
_DESTRUCTIVE_RE = re.compile(
    r"""(?ix)
      \b(rm|del|delete|remove|erase|wipe|purge|shred|trash|format|drop|unlink)\b
    | \b(overwrite|clobber|replace\s+(the\s+)?file)\b
    | \b(kill|terminate|shutdown|reboot|halt|stop\s+the\s+(process|service))\b
    | \b(git\s+(push|reset|clean|checkout|rm|branch\s+-D))\b
    | \b(chmod|chown|mkfs|sudo|su\s)\b
    | \b(install|pip\s+install|npm\s+(i|install)|curl\b.*\|\s*(sh|bash))\b
    | \b(deploy|migrate\s+database|drop\s+database)\b
    """
)

# Action verbs — anything that would make Hermes DO something on the machine
# or online. A task containing none of these is pure conversation (chat, QA,
# acknowledging a codename) and needs no confirm gate. Conservative by design:
# when in doubt the verb list wins and the task stays gated.
_ACTION_RE = re.compile(
    r"""(?ix)
      \b(create|make|build|generate|write|save|update|edit|modify|rename|move|copy|paste|
         delete|remove|erase|wipe|purge|trash|format|drop|unlink|install|uninstall|
         git|pip|npm|deploy|migrate|chmod|chown|sudo|
         open|launch|run|execute|start|restart|stop|kill|close|shutdown|reboot|
         send|post|email|tweet|upload|download|
         click|type|press|scroll|drag|swipe|
         browse|visit|navigate|scrape|search|find|lookup|query|check|
         play|pause|skip|mute|volume|
         scan|rescan|refresh|compile|test|debug|
         change|set|toggle|switch|enable|disable|cancel|book|buy|order|pay|
         subscribe|submit|approve|accept|deny|allow|block)\b
    """
)

def is_conversational(task: str) -> bool:
    """Pure chat/QA — no mutating or action verb, so no executor is needed.

    Used by the brain to skip Hermes delegation for ChatGPT-style turns
    ("who am I?", "what is your codename?") and by the confirm gate (safe).
    """
    t = str(task or "").strip()
    if not t or _MUTATING_RE.search(t) or _ACTION_RE.search(t):
        return False
    return len(t.split()) <= 40


def _is_safe_task(task: str) -> bool:
    """True if task is pre-approved safe (allowlist) and not mutating."""
    t = (task or "").strip()
    if not t:
        return False
    # Mutating always needs confirm, even if phrase looks safe.
    if _MUTATING_RE.search(t):
        return False
    # An instruction ("empty my recycle bin", "can you nuke X?") is safe only
    # through the allowlist. The question-mark and no-listed-verb exemptions
    # below are for chat; before 2026-09-11 an instruction with an unlisted
    # verb took them and ran with no confirm.
    if not _reads_as_command(t):
        # Informational Q&A without explicit verb is safe if it ends with ? or is short question.
        if t.endswith("?") and len(t.split()) <= 20:
            return True
        # Conversational exemption: no action verb anywhere -> there is nothing
        # for the executor to DO on the machine, so answering is safe. (Verified
        # live: a harmless "my codename is X, acknowledge it" was gated as
        # destructive, costing a confirm + a 2-min Hermes round trip per chat turn.)
        if is_conversational(t):
            return True
    # An instruction to fix or edit something changes files, whatever else it
    # says: "... fix the chat buttons ... double check it" matched "check" on
    # the allowlist and ran as a look-only job that only described the fix.
    if _CHANGE_RE.search(t):
        return False
    core = _REQUEST_LEAD_RE.sub("", t)
    return bool(_SAFE_ALLOWLIST_RE.search(t) or _SAFE_ALLOWLIST_RE.search(core))

# Module-level latch so a confirm only releases the EXACT pending task.
_PENDING_DESTRUCTIVE = {"task": None}

# Full delegate_to_hermes kwargs captured when a task is gated, so a bare
# "confirm" utterance (which arrives as a NEW turn with different text and
# can never text-match the latch) can re-run the EXACT stored call. Without
# this, spoken confirms just created new waiting-on-confirm jobs forever.
_PENDING_HERMES_CALL: dict | None = None

# Phase 14: same latch pattern for desktop-control actions. EVERY
# desktop_control call is gated — no lexical detection, unconditional.
_PENDING_DESKTOP = {"task": None, "foreground": False}

# A confirm answers the question just asked. The job card timed out after
# jobs.STALE_TTL_SECONDS while the latch stayed armed, so a "confirm" hours
# later still ran the task (2026-09-13). They now expire together.
_LAST_EXPIRED = {"task": None, "at": 0.0}


def _expire_confirms() -> None:
    """Drop the Hermes-family and autonomous latches once their confirm
    window has passed, remembering what was dropped for the next "confirm"."""
    global _PENDING_HERMES_CALL
    from cygnus.jobs import registry as _jobs
    now = time.time()
    hermes = _PENDING_HERMES_CALL
    if hermes and hermes.get("ts") and now - hermes["ts"] > _jobs.STALE_TTL_SECONDS:
        _PENDING_HERMES_CALL = None
        _PENDING_DESTRUCTIVE["task"] = None
        _LAST_EXPIRED.update(task=hermes.get("raw_task") or hermes.get("task"), at=now)
    goal, goal_ts = _PENDING_DESTRUCTIVE.get("autonomous"), _PENDING_DESTRUCTIVE.get("autonomous_ts")
    if goal and goal_ts and now - goal_ts > _jobs.STALE_TTL_SECONDS:
        _PENDING_DESTRUCTIVE["autonomous"] = None
        _PENDING_DESTRUCTIVE["autonomous_ts"] = 0.0
        _LAST_EXPIRED.update(task=goal, at=now)


def expired_confirm_reply() -> str | None:
    """What a "confirm" that came too late is told, once; None otherwise."""
    _expire_confirms()
    task = _LAST_EXPIRED.get("task")
    if not task or time.time() - _LAST_EXPIRED["at"] > 3600:
        return None
    _LAST_EXPIRED.update(task=None, at=0.0)
    return (f"That request timed out before you confirmed it, sir, so I didn't run "
            f"“{str(task)[:80]}”. Ask me again if you still want it.")


# Hard-blocked content classes: never executed even with confirmation.
# Mirrors cua-driver policy; checked on the task text before release.
_DESKTOP_BLOCKED_RE = re.compile(
    r"""(?ix)
      \b(password|passwd|passcode|pin|credit\s*card|cvv|otp|2fa)\b
    | \b(pay|payment|checkout|purchase|buy)\b.*\b(now|confirm|submit)\b
    | \b(allow|grant|accept)\b.*\b(permission|dialog|uac|admin)\b
    | \b(format|wipe)\b.*\b(disk|drive)\b
    """)


def desktop_control(task: str, confirm: bool = False,
                    foreground: bool = False) -> str:
    """Operate a desktop app via Hermes computer_use (Phase 14).

    Every action requires voice confirmation: the first call returns
    NEEDS_CONFIRM describing exactly what will be done; only a second call
    with confirm=True releases THAT task (latched). Background delivery by
    default; foreground takeover only when explicitly requested.
    """
    task = str(task or "").strip()
    if not task:
        return "[Error] No desktop action given."

    if _DESKTOP_BLOCKED_RE.search(task):
        return ("[Blocked] That action touches credentials, payments or "
                "system dialogs — I won't do it even with confirmation.")

    fg = bool(foreground)

    # Confirmation gate: unconditional for this tool.
    if not confirm:
        _PENDING_DESKTOP["task"] = task
        _PENDING_DESKTOP["foreground"] = fg
        mode = "in the FOREGROUND (takes over your mouse)" if fg \
            else "in the background"
        return (f"NEEDS_CONFIRM: I will: {task} ({mode}). "
                "Say 'confirm' to proceed.")

    # Latch check: a confirm releases ONLY the exact pending action.
    if _PENDING_DESKTOP.get("task") != task:
        return ("[Error] Nothing pending matches that action. "
                "State it again so I can re-confirm what will happen.")

    pending_fg = _PENDING_DESKTOP.get("foreground", False)
    _PENDING_DESKTOP["task"] = None
    _PENDING_DESKTOP["foreground"] = False

    instruction = (
        "Use your computer_use tooling to perform this desktop action and "
        "report what you actually did with evidence from a post-action "
        "capture. Delivery: " + ("foreground" if pending_fg else "background")
        + (" (do NOT raise windows or steal focus; use background input "
           "delivery)." if not pending_fg else ".")
        + " Never touch password fields, payment UIs or OS permission dialogs. "
        + "TASK: " + task)
    result = delegate_to_hermes(
        instruction, timeout=300, max_turns=15, confirm=False,
        raw_task=task)
    tag = "[desktop-confirmed" + (", foreground" if pending_fg else "") + "]"
    return f"{result} {tag}" if not str(result).startswith("[Error]") else result


def _desktop_control_tool(**kw):
    return desktop_control(kw.get("task"), _truthy(kw.get("confirm", False)),
                           _truthy(kw.get("foreground", False)))


# ---------------------------------------------------------------------------
# OpenCLI integration (Phase 3.6): "turn any website into a CLI" via the user's
# logged-in Chrome. Built 2026-08-26 from an OpenCLI Facebook post the user
# wanted JARVIS to use. OpenCLI is a third-party npm CLI (jackwener/OpenCLI,
# Apache-2.0); JARVIS shells out to it.
#
# SAFETY MODEL (mirrors Phase 14 desktop_control):
#   - EVERY write / logged-in-site action requires an explicit confirm gate.
#   - Public-site READ commands (wikipedia, arxiv, github trending, etc.) run
#     through audit but are treated as low-risk (no login needed; the daemon
#     serves them). They still require a one-shot confirm the FIRST time a
#     session touches the tool, so the user is never surprised JARVIS is driving
#     a browser.
#   - Hard-blocked: credentials / payments / anything the _DESKTOP_BLOCKED_RE
#     already forbids (reused) — JARVIS never types into login/payment fields
#     even with confirm.
#   - Delivery is background by default (--window background).
# ---------------------------------------------------------------------------
import re as _re

_OPENCLI_WRITE_RE = _re.compile(
    r"\b(add-friend|join-group|login|post|send|comment|reply|upload|create|delete|"
    r"remove|follow|unfollow|like|share|message|dm|publish|submit|write|update|"
    r"marketplace|inbox|settings|account|profile edit|set-)\b", _re.IGNORECASE)
_OPENCLI_SAFE_READ_RE = _re.compile(
    r"\b(feed|search|summary|page|random|trending|recent|whoami|notifications|"
    r"profile|list|get|read|events|friends|groups|memories|paper|author|status|"
    r"listings)\b", _re.IGNORECASE)
# Sites whose adapters imply driving a LOGGED-IN session even on a "read".
_OPENCLI_SENSITIVE_SITE_RE = _re.compile(
    r"\b(facebook|instagram|twitter|x\b|reddit|linkedin|weibo|douyin|xiaohongshu|"
    r"zhihu|tiktok|github|gmail|notion|discord|telegram|wechat|slack|bank|gov)\b",
    _re.IGNORECASE)

# Module-level latch: a confirm releases ONLY the exact pending command.
_PENDING_OPENCLI = {"command": None}


def _opencli_classify(command: str):
    """Return (kind, needs_confirm) where kind in {read, write, sensitive-read}."""
    cmd = command.strip()
    low = cmd.lower()
    # A bare `opencli <site>` with no subcommand + a sensitive site => sensitive.
    if _OPENCLI_SENSITIVE_SITE_RE.search(low):
        # explicit read verb on a sensitive site still needs confirm (it reads
        # YOUR data); anything with a write verb is a hard write.
        if _OPENCLI_WRITE_RE.search(low):
            return "write", True
        return "sensitive-read", True
    if _OPENCLI_WRITE_RE.search(low):
        return "write", True
    if _OPENCLI_SAFE_READ_RE.search(low) or low.split():
        return "read", True  # first-touch confirm; public reads are low risk
    return "read", True


def run_opencli(command: str, confirm: bool = False,
                foreground: bool = False) -> str:
    """Run an OpenCLI command (turn a website into a CLI via logged-in Chrome).

    First call returns NEEDS_CONFIRM with the exact command + risk class.
    Second call with confirm=true runs it. Unconditional gate (Phase 14 style):
    JARVIS must never silently drive a browser/site.
    """
    command = str(command or "").strip()
    if not command:
        return "[Error] No OpenCLI command given (e.g. 'reddit search python')."

    if _DESKTOP_BLOCKED_RE.search(command):
        return ("[Blocked] That OpenCLI command touches credentials, payments or "
                "system dialogs — I won't run it even with confirmation.")

    kind, needs_confirm = _opencli_classify(command)
    risk = {"read": "public read", "sensitive-read": "LOGGED-IN read",
            "write": "WRITE/action"}[kind]

    if not confirm:
        _PENDING_OPENCLI["command"] = command
        return (f"NEEDS_CONFIRM [OpenCLI · {risk}]: I will run: `opencli {command}`. "
                f"Say 'confirm' to proceed. This may operate your browser/sites.")

    if _PENDING_OPENCLI.get("command") != command:
        return ("[Error] Nothing pending matches that OpenCLI command. "
                "State it again so I can re-confirm what will run.")

    _PENDING_OPENCLI["command"] = None

    import subprocess
    import shlex
    # `--window` is only valid for the `opencli browser ...` primitive, NOT for
    # site/app adapters (they reject it with "unknown option '--window'").
    # Default delivery is background for browser primitives; site adapters run
    # in whatever session the daemon/extension has bound.
    is_browser_primitive = command.lower().startswith("browser")
    base = f"opencli {command}" + (
        f" --window {'foreground' if foreground else 'background'}"
        if is_browser_primitive else "")
    # Run opencli's own script, not its .cmd shim through a shell: cmd.exe
    # reads & | in the command as its syntax (see _direct_launcher). Quoted
    # multi-word args stay ONE argument: shlex.split() discards the user's
    # quotes, so a quote-aware splitter keeps them, and they are removed as
    # each argument is handed over.
    launch = _direct_launcher("opencli")
    if not launch:
        return "[OpenCLI error] opencli is not installed on PATH."

    def _split_keep_quotes(s):
        out, cur, q = [], "", None
        for ch in s:
            if q:
                cur += ch
                if ch == q:
                    q = None
            elif ch in ('"', "'"):
                q = ch
                cur += ch
            elif ch.isspace():
                if cur:
                    out.append(cur); cur = ""
            else:
                cur += ch
        if cur:
            out.append(cur)
        return out

    def _unquote(tok):
        return tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok

    raw_tokens = [_unquote(t) for t in _split_keep_quotes(base)]
    # For site/app adapters (NOT the `browser` primitive), everything after
    # the 2nd token is free text (a title/query) that OpenCLI wants as ONE
    # argument. Rejoin trailing tokens so Web scraping -> "Web scraping".
    if not is_browser_primitive and len(raw_tokens) > 3:
        raw_tokens = raw_tokens[:3] + [" ".join(raw_tokens[3:])]
    argv = launch + raw_tokens[1:]              # raw_tokens[0] is "opencli"
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=180,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            tail = (err or out or "no output")[-600:]
            _audit_log("run_opencli", command, "fail", result=f"rc={proc.returncode} {tail}")
            return f"[OpenCLI error rc={proc.returncode}] {tail}"
        # Evidence: only quote a bounded, real slice.
        shown = out[:1500]
        _audit_log("run_opencli", command, "ok", result=shown[:240])
        tag = f"[opencli-{kind}]"
        return f"{tag} {shown}" + ("" if len(out) <= 1500 else f"\n…({len(out)-1500} more chars)")
    except subprocess.TimeoutExpired:
        return f"[OpenCLI timeout] `opencli {command}` exceeded 180s."
    except Exception as e:  # pragma: no cover - defensive
        return f"[OpenCLI error] {type(e).__name__}: {e}"


def _run_opencli_tool(**kw):
    return run_opencli(kw.get("command"), _truthy(kw.get("confirm", False)),
                       _truthy(kw.get("foreground", False)))


# Phase 4: fire-and-forget acknowledgment token. When Hermes is delegated in the
# background, think() returns this immediately so the voice/mic path stays live;
# the real answer arrives later via the on_done callback.
HERMES_BACKGROUND_ACK = "⟳ HERMES_BACKGROUND: On it, sir, working on that now…"


def _run_hermes_sync(task: str, timeout: int, max_turns: int,
                      progress_cb=None) -> str:
    """Synchronously launch Hermes and return its parsed reply (blocking).

    Each job runs in a Hermes session of its own: resuming one shared session
    carried every earlier job into the next (49 messages, ~70k tokens a call,
    and an unrelated CLAUDE_TASK.md read on 2026-09-13).
    Phase 4: progress_cb streams milestone status to the caller (WS/voice).
    """
    from cygnus.jobs.warm_harness import warm_send
    return warm_send(task, timeout=timeout, max_turns=max_turns,
                     progress_cb=progress_cb, fresh=True)


# A "safe" task is one the user asked to look at, not change, so Hermes is
# told so. Its toolsets cannot be narrowed to reading (the file toolset reads
# and writes), so Cygnus's own folder is checked afterwards as well: on
# 2026-09-12 a look-only job rewrote jarvis_hud_v3.html and nobody was told.
_LOOK_ONLY_BRIEF = ("LOOK-ONLY TASK: read, search and report back. Do not create, edit, "
                    "move or delete any file, and do not run anything that changes the "
                    "machine. If the task needs a change, say what you would change.\n\n")


def _repo_changes() -> dict:
    """{path: mtime} for every file git sees as changed in Cygnus's folder."""
    root = str(paths.ROOT)
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return {}
    seen = {}
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        try:
            seen[path] = os.path.getmtime(os.path.join(root, path))
        except OSError:
            seen[path] = None
    return seen


def _changed_files(before: dict) -> list:
    """Files in Cygnus's folder changed since the `before` snapshot."""
    return sorted(p for p, m in _repo_changes().items() if before.get(p, -1) != m)


# A changing task whose reply only says what would be done ("I would delegate
# this to Claude Code. The goal would be ...") did not do it. On 2026-09-13
# such a reply was marked Done.
_DESCRIBES_ONLY_RE = re.compile(r"\s*(?:I would|I'd|The goal (?:\w+ ){0,3}would be)\b", re.I)
DESCRIBED_ONLY_NOTE = "only described the work; no files in Cygnus's folder changed"


def _finish_hermes_job(jid, result: str, before: dict, look_only: bool) -> None:
    """Mark a finished Hermes job done, or unverified when a changing task
    came back as a description of the work and nothing changed."""
    from cygnus.jobs import registry as _jobs
    if not look_only and _DESCRIBES_ONLY_RE.match(result) and not _changed_files(before):
        _jobs.update(jid, state="unverified", note=DESCRIBED_ONLY_NOTE, result=result, summary=result[:200])
    else:
        _jobs.update(jid, state="done", result=result, summary=result[:200])


def _look_only_report(before: dict, result: str, jid) -> str:
    """`result`, plus a plain note if files changed while a look-only job ran."""
    changed = _changed_files(before)
    if not changed:
        return result
    note = "Files in Cygnus's folder changed while this look-only job ran: " + ", ".join(changed[:8])
    _audit_log("delegate_to_hermes", note, "look_only_changed_files", extra={"jid": jid})
    try:
        from cygnus.jobs import registry as _jobs
        _jobs.update(jid, note=note)
    except Exception:
        pass
    return f"{result}\n\n[{note}]"


def delegate_to_hermes(task: str, timeout: int = 300, max_turns: int = 15,
                       confirm: bool = False, raw_task: str = None,
                       background: bool = False, on_done=None,
                       progress_cb=None, grounded: bool = False, jid: str = None) -> str:
    """Hand a task to the local Hermes agent (full toolset) and report its answer.

    Hermes is the executor: it can use the terminal, files, browser, code,
    subagents, and more to actually do the work on this machine.

    APPROVAL GATE: if the task looks destructive (delete/overwrite/install/git
    push/kill/sudo/...), the first call does NOT launch Hermes. It returns a
    NEEDS_CONFIRM notice. Call again with the SAME task and confirm=true to run
    it. Read-only / safe tasks run immediately with no confirmation.

    `raw_task` (internal): when a wrapper prepends context (profile/session/
    grounding) to `task`, the destructive check must run on the USER's original
    words, not on the injected context (which legitimately contains words like
    "delete"/"install" inside the safety instructions and would false-positive).
    If provided, the gate is evaluated against `raw_task`.

    `progress_cb` (Phase 4): callable invoked with status strings at milestones
    ("Delegating to Hermes...", "Hermes finished.") so the caller can stream
    progress to the user while Hermes runs.

    ASYNC / fire-and-forget (Phase 4): when `background=True` and `on_done` is a
    callable, Hermes is launched in a daemon thread and this returns IMMEDIATELY
    with a "working" acknowledgment token (HERMES_BACKGROUND_ACK). The real answer
    is delivered later by calling `on_done(result)`. This keeps the voice/mic path
    responsive instead of freezing for the ~2-min Hermes cold start. The sync path
    (background=False) is unchanged and blocks until Hermes returns.
    """
    task = str(task or "").strip()
    if not task:
        return "[Error] No task given to delegate to Hermes."

    # Gate the user's real intent, never the injected context.
    gate_target = raw_task if raw_task is not None else task
    gate_target = str(gate_target or "").strip()
    if _is_not_a_task(gate_target):
        return _not_a_task_reply(gate_target)

    try:
        timeout = max(15, min(int(timeout), 600))
    except (TypeError, ValueError):
        timeout = 300
    try:
        max_turns = max(1, min(int(max_turns), 30))
    except (TypeError, ValueError):
        max_turns = 6

    # Phase 1: Allowlist gate — only pre-approved safe tasks run without confirm.
    # Mutating/file-changing tasks always need confirm (load-bearing before Phases 2+7).
    is_safe = _is_safe_task(gate_target)
    needs_confirm = not is_safe
    _audit_log("delegate_to_hermes", gate_target, "safe" if is_safe else "needs_confirm", confirm=confirm, extra={"is_safe": is_safe, "needs_confirm": needs_confirm, "timeout": timeout, "max_turns": max_turns})
    # Phase 2: session registry — every delegation gets a job ID, non-blocking, proactive.
    from cygnus.jobs import registry as _jobs
    # create job early so waiting-on-confirm is also tracked (queryable, not hidden in latch)
    # `jid`: the card that waited for this confirm carries on (no new card).
    jid = jid or _jobs.create(gate_target[:200], tier="hermes", background=background)
    if needs_confirm:
        global _PENDING_HERMES_CALL
        if not confirm:
            _PENDING_DESTRUCTIVE["task"] = task
            _PENDING_HERMES_CALL = dict(
                task=task, timeout=timeout, max_turns=max_turns, raw_task=raw_task,
                background=background, on_done=on_done, progress_cb=progress_cb, jid=jid,
                ts=time.time(), grounded=grounded,
                # Whose question this is: only that device may answer it.
                client=_current_client())
            _jobs.update(jid, state="waiting-on-confirm", note="needs confirm — say 'confirm' to run", summary=f"Waiting for confirm: {gate_target[:120]}")
            _audit_log("delegate_to_hermes", gate_target, "NEEDS_CONFIRM", confirm=False, extra={"jid": jid})
            return (f"[NEEDS_CONFIRM:{jid}] That task changes state or isn't on the pre-approved safe list. "
                    f"If you want me to proceed, say or type 'confirm' and I'll run it through Hermes: "
                    f"\"{gate_target}\" (job {jid})")
        if _PENDING_DESTRUCTIVE["task"] != task:
            _PENDING_DESTRUCTIVE["task"] = task
            _PENDING_HERMES_CALL = dict(
                task=task, timeout=timeout, max_turns=max_turns, raw_task=raw_task,
                background=background, on_done=on_done, progress_cb=progress_cb, jid=jid,
                ts=time.time(), grounded=grounded,
                # Whose question this is: only that device may answer it.
                client=_current_client())
            _jobs.update(jid, state="waiting-on-confirm", note="latch mismatch — re-issue exact task then confirm")
            _audit_log("delegate_to_hermes", gate_target, "NEEDS_CONFIRM_latch_mismatch", confirm=True, extra={"jid": jid})
            return ("[NEEDS_CONFIRM] Please re-issue the exact task and then "
                    "confirm, so I run the right one.")
        _PENDING_DESTRUCTIVE["task"] = None
        _PENDING_HERMES_CALL = None
        _jobs.update(jid, state="running", note="confirmed — launching Hermes")
        _audit_log("delegate_to_hermes", gate_target, "confirmed", confirm=True, extra={"jid": jid})
    else:
        _jobs.update(jid, state="running", note="safe — launching Hermes")
        task = _LOOK_ONLY_BRIEF + task
    before = _repo_changes()

    # --- Execution path -----------------------------------------------------
    # Phase 2+4: non-blocking. Front never blocks; status via jobs registry + WS listener.
    if background and callable(on_done):
        jid_bg = jid
        def _bg():
            j = jid_bg
            try:
                result = _run_hermes_sync(task, timeout, max_turns, progress_cb)
                if is_safe:
                    result = _look_only_report(before, result, j)
                if result.startswith("[Error]"):
                    _jobs.update(j, state="error", error=result[:500], result=result, summary=result[:120])
                    _audit_log("delegate_to_hermes", gate_target, "background_error", result=result, confirm=confirm, extra={"jid": j})
                else:
                    _finish_hermes_job(j, result, before, is_safe)
                    _audit_log("delegate_to_hermes", gate_target, "background_done", result=result, confirm=confirm, extra={"jid": j})
            except Exception as e:
                result = f"[Error] Hermes background task failed: {type(e).__name__}: {e}"
                _jobs.update(j, state="error", error=str(e)[:500], result=result, summary=str(e)[:120])
                _audit_log("delegate_to_hermes", gate_target, "background_error", result=result, confirm=confirm, extra={"jid": j})
            try:
                on_done(result)
            except Exception as e:
                print(f"[HERMES] on_done callback raised: {e}", flush=True)
        threading.Thread(target=_bg, daemon=True).start()
        _audit_log("delegate_to_hermes", gate_target, "background_queued", confirm=confirm, extra={"jid": jid})
        return f"{HERMES_BACKGROUND_ACK} (job {jid})"

    # sync path — still tracked in registry so HUD shows it even while blocked
    _jobs.update(jid, state="running", note="sync execution")
    result = _run_hermes_sync(task, timeout, max_turns, progress_cb)
    if is_safe:
        result = _look_only_report(before, result, jid)
    if result.startswith("[Error]"):
        _jobs.update(jid, state="error", error=result[:500], result=result, summary=result[:120])
    else:
        _finish_hermes_job(jid, result, before, is_safe)
    _audit_log("delegate_to_hermes", gate_target, "executed", result=result, confirm=confirm, extra={"jid": jid})
    return result


def _current_client() -> str:
    """Which HUD this turn came from; safety.py owns the answer."""
    from cygnus import safety
    return safety.current_client()


def _same_client(latched) -> str:
    """May this turn answer a question held for `latched`? safety.same_client."""
    from cygnus import safety
    return safety.same_client(latched)


def pending_confirm_task() -> str | None:
    """The raw user task this device was asked to confirm, or None."""
    _expire_confirms()
    kw = _PENDING_HERMES_CALL or {}
    if not _same_client(kw.get("client")):
        return None
    return kw.get("raw_task")


def pending_confirm_elsewhere() -> str | None:
    """The task waiting on ANOTHER device, or None.

    A "yes" here answers nothing, and saying so beats routing it as a fresh
    request (2026-09-23: a desktop "yes" launched a delete held for the phone).
    """
    _expire_confirms()
    kw = _PENDING_HERMES_CALL or {}
    if not kw or _same_client(kw.get("client")):
        return None
    return kw.get("raw_task") or kw.get("task")


def decline_pending() -> str | None:
    """The user said no to the confirm they just heard: drop that latch (the
    Hermes task or the autonomous goal, whichever was asked last) and close
    its job. None when nothing is waiting - the caller routes normally.

    Before this, "no" reached the model, which answered "I have cancelled
    that request" while the task stayed latched, and "no, cancel that" lost
    its "no," to the correction stripper and became a NEW gated task."""
    global _PENDING_HERMES_CALL
    _expire_confirms()
    hermes = _PENDING_HERMES_CALL
    if hermes and not _same_client(hermes.get("client")):
        hermes = None            # another device's question, not this "no"
    goal = _PENDING_DESTRUCTIVE.get("autonomous")
    if not hermes and not goal:
        return None
    goal_ts = _PENDING_DESTRUCTIVE.get("autonomous_ts") or 0.0
    if goal and (not hermes or goal_ts >= (hermes.get("ts") or 0.0)):
        _PENDING_DESTRUCTIVE["autonomous"] = None
        _PENDING_DESTRUCTIVE["autonomous_ts"] = 0.0
        what = goal
    else:
        what = hermes.get("raw_task") or hermes.get("task") or ""
        _PENDING_HERMES_CALL = None
        _PENDING_DESTRUCTIVE["task"] = None
        if hermes.get("jid"):
            from cygnus.jobs import registry as _jobs
            _jobs.update(hermes["jid"], state="cancelled", note="declined by the user")
    _audit_log("delegate_to_hermes", what, "declined", confirm=False)
    return f"Cancelled, sir. I won't run “{what[:80]}”."


def pending_autonomous_goal() -> str | None:
    """The goal currently latched for autonomous-run confirmation, or None."""
    _expire_confirms()
    return _PENDING_DESTRUCTIVE.get("autonomous")


def confirm_pending(on_done=None, progress_cb=None, background: bool | None = None) -> str | None:
    """Release the confirm latch and run the pending task with confirm=True.

    Re-invokes delegate_to_hermes with the EXACT stored kwargs (including the
    context-injected task string), so the _PENDING_DESTRUCTIVE latch matches
    and the task actually launches instead of queuing another gated job.
    on_done/progress_cb/background can be overridden with the CURRENT turn's
    callbacks (the captured ones belong to the turn that requested confirm).
    Returns None when nothing is pending — caller falls through to normal
    routing.
    """
    global _PENDING_HERMES_CALL
    _expire_confirms()
    kw = _PENDING_HERMES_CALL
    if not kw:
        return None
    if not _same_client(kw.get("client")):
        # Asked on another device; leave it held for that one.
        _audit_log("delegate_to_hermes", (kw.get("raw_task") or "")[:200],
                   "confirm_from_another_client", confirm=False,
                   extra={"jid": kw.get("jid")})
        return None
    if kw.get("cli"):                    # a CLI agent waited: its own card runs on
        _PENDING_HERMES_CALL = None
        _PENDING_DESTRUCTIVE["task"] = None
        _audit_log(f"delegate:{kw['cli']}", (kw.get("raw_task") or "")[:200], "confirmed",
                   confirm=True, extra={"jid": kw.get("jid")})
        return _start_cli_job(kw["task"], kw["cli"], raw_task=kw.get("raw_task"),
                              on_done=on_done or kw.get("on_done"), jid=kw.get("jid"))
    if kw.get("spec"):                   # an OpenCode specialist or Pi waited
        _PENDING_HERMES_CALL = None
        _PENDING_DESTRUCTIVE["task"] = None
        _audit_log(f"delegate:{kw['spec']}", (kw.get("raw_task") or "")[:200], "confirmed",
                   confirm=True, extra={"jid": kw.get("jid")})
        if kw.get("jid"):
            from cygnus.jobs import registry as _jobs
            _jobs.update(kw["jid"], state="timeout",
                         note="superseded — confirmed, running as a new job")
        agent = None if kw["spec"] == "opencode" else kw["spec"]
        bg = kw.get("background") if background is None else background
        return _run_specialist_now(kw["task"], agent, kw.get("pi", False), kw.get("timeout", 300),
                                   bg, on_done or kw.get("on_done"))
    superseded_jid = kw.get("jid")
    # "client" is bookkeeping about who was asked, not an argument of the call.
    kw = {k: v for k, v in kw.items() if k not in ("jid", "ts", "client")}
    if on_done is not None:
        kw["on_done"] = on_done
    if progress_cb is not None:
        kw["progress_cb"] = progress_cb
    if background is not None:
        kw["background"] = background
    if kw.pop("grounded", False):         # asked before its brief was built: build it now
        return _confirm_grounded(kw, superseded_jid)
    if superseded_jid:
        from cygnus.jobs import registry as _jobs
        _jobs.update(superseded_jid, state="timeout",
                     note="superseded — re-confirmed, running as a new job")
    return delegate_to_hermes(confirm=True, **kw)


def _confirm_grounded(kw: dict, jid: str | None) -> str:
    """Run a confirmed task whose brief was not built for the question: build
    it now, on the card that waited, in the background when the turn allows,
    so "confirm" is answered at once instead of after the ~37 s brief."""
    from cygnus.jobs import registry as _jobs
    if jid:
        _jobs.update(jid, state="running", note="getting ready")
    raw = kw.get("raw_task") or kw["task"]

    def run():
        return delegate_to_hermes_grounded(raw, timeout=kw["timeout"], max_turns=kw["max_turns"],
                                           confirm=True, progress_cb=kw.get("progress_cb"), jid=jid)

    on_done = kw.get("on_done")
    if not (kw.get("background") and callable(on_done)):
        return run()

    def _bg():
        try:
            result = run()
        except Exception as e:
            result = f"[Error] Hermes task failed: {type(e).__name__}: {e}"
        try:
            on_done(result)
        except Exception as e:
            print(f"[HERMES] on_done callback raised: {e}", flush=True)

    threading.Thread(target=_bg, daemon=True, name=f"hermes-confirm-{jid}").start()
    return f"{HERMES_BACKGROUND_ACK} (job {jid})" if jid else HERMES_BACKGROUND_ACK


# ── Grounded Hermes delegation (Pillar C: memory layer) ─────────────────────
# Wraps delegate_to_hermes for JARVIS's use: injects the Tier-2 profile + Tier-3
# rolling session context into the task so Hermes has continuity and ground truth,
# plus an instruction to ground answers in the Second Brain vault and cite sources.
# Keep this import-local so importing tools.py never hard-fails if session_store
# is absent on some deployment.
_PROFILE_PATH = str(paths.PROFILE)
_VAULT_HINT = ("Second Brain vault at C:/Users/deped/Documents/Second Brain "
               "(semantic search via turbovec; read/write wiki/evergreen).")


def _load_profile() -> str:
    try:
        with open(_PROFILE_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "(jarvis-profile.md not found)"


def _app_context(task: str) -> str:
    """If the task names any installed app, append resolved launch locations + a
    how-to so the delegated Hermes drives the EXACT path (via its terminal tool)
    instead of guessing. computer_use is a skill, not a loaded tool in the harness,
    so deterministic path/URI guidance is what makes app control actually work."""
    try:
        from cygnus.apps.capabilities import resolve as _resolve
    except Exception:
        try:
            from cygnus.apps.capabilities import resolve as _resolve
        except Exception:
            return ""
    # Known app keywords to probe (broad; resolve() does fuzzy match).
    probes = ["spotify", "vlc", "chrome", "brave", "edge", "firefox", "notepad",
              "word", "excel", "powerpoint", "discord", "telegram", "slack",
              "whatsapp", "teams", "zoom", "obs", "blender", "gimp", "vscode",
              "code", "terminal", "calculator", "photos", "paint", "explorer",
              "steam", "epic", "netflix", "youtube", "music", "browser", "app"]
    found = []
    low = (task or "").lower()
    for p in probes:
        if p in low:
            e = _resolve(p)
            if e:
                line = f"- {e.get('name', p)} ({e.get('kind')}): {e.get('bin')}"
                if "spotify" in e.get("name", "").lower():
                    line += ("  | music search: run `start spotify:search:QUERY` "
                             "(opens the Search pane); then the user picks & plays.")
                found.append(line)
    if not found:
        return ""
    return ("\n[INSTALLED APPS — launch these by EXACT path/URI via your terminal tool]\n"
            + "\n".join(found) +
            "\nTo open: `start \"\" \"<path>.lnk\"` or `start \"\" \"<path>.exe\"`. "
            "Do NOT guess paths.\n")


def delegate_to_hermes_grounded(task: str, timeout: int = 300, max_turns: int = 15,
                                 confirm: bool = False, background: bool = False,
                                 on_done=None, progress_cb=None, jid: str = None) -> str:
    """Delegate to Hermes WITH per-delegate context assembly (Phase 4).
    Uses context_assembler.assemble_brief for hermes formatting, logs brief.

    A task that needs a confirm asks first and gathers after: building the
    brief (vault search, session index) took 37-60 s before "please confirm"
    was even said, long enough to drop the client's socket (2026-09-12). The
    latch remembers the task is grounded; confirm_pending builds the brief.
    """
    if not confirm and not _is_safe_task(task):
        return delegate_to_hermes(task, timeout=timeout, max_turns=max_turns, confirm=False,
                                  raw_task=task, background=background, on_done=on_done,
                                  progress_cb=progress_cb, grounded=True)
    # The task restated and first, the words as spoken next, the background
    # last (context_assembler.compose_prompt, 2026-09-24).
    try:
        from cygnus.brain import context_assembler as ca
        prompt = ca.compose_prompt(task, delegate="hermes", raw_task=task)
    except Exception:
        # fallback to legacy assembly if assembler missing
        from cygnus.memory.session_store import recent_summary
        profile = _load_profile()
        recent = recent_summary(6)
        appctx = _app_context(task)
        prefix = (
            "CONTEXT (Cygnus persistent memory):\n"
            f"[PROFILE]\n{profile}\n\n"
            f"[RECENT SESSION]\n{recent}\n\n"
            + f"[KNOWLEDGE BASE] {_VAULT_HINT}\n"
            "Ground factual answers in the Second Brain vault (semantic search it) and "
            "cite the source note name. If nothing relevant, say so. Respect [PROFILE].\n\n"
        )
        if appctx:
            prefix += appctx + "\n"
        prompt = prefix + "TASK:\n" + task
    if confirm and _PENDING_DESTRUCTIVE.get("task") == task:
        # The user confirmed this exact task: the latch now holds the briefed
        # text that is about to run, so the confirm releases it.
        _PENDING_DESTRUCTIVE["task"] = prompt
    return delegate_to_hermes(prompt, timeout=timeout, max_turns=max_turns,
                              confirm=confirm, raw_task=task,
                              background=background, on_done=on_done,
                              progress_cb=progress_cb, jid=jid)


# ── Phase 3: Unified delegate() — single routing primitive ────────────────
# Collapses delegate_task / delegate_to_hermes / delegate_to_hermes_grounded
# into ONE function. The router picks the backend; delegate() executes.

_DELEGATE_REGISTRY_PATH = str(paths.DELEGATE_REGISTRY)
_DELEGATE_CACHE = None
_DELEGATE_CACHE_MTIME = 0

def _load_delegate_registry():
    """Load delegate_registry.json with mtime cache. Returns dict or None."""
    global _DELEGATE_CACHE, _DELEGATE_CACHE_MTIME
    try:
        mtime = os.path.getmtime(_DELEGATE_REGISTRY_PATH)
        if _DELEGATE_CACHE is not None and mtime == _DELEGATE_CACHE_MTIME:
            return _DELEGATE_CACHE
        with open(_DELEGATE_REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _DELEGATE_CACHE = data
        _DELEGATE_CACHE_MTIME = mtime
        return data
    except Exception:
        return None

def _detect_backend(task: str) -> str:
    """The backend for a delegated task; see _detect_backend_by_task. Manus is
    never picked while it has no cookie file: "make it 100% volume" was sent
    there and answered "[Manus] Not configured" (2026-09-11)."""
    backend = _detect_backend_by_task(task)
    if backend == "manus":
        try:
            from cygnus.browser import manus as manus_agent
            if not manus_agent._cookies_present():
                return "hermes"
        except Exception:
            return "hermes"
    return backend


def _detect_backend_by_task(task: str) -> str:
    """Auto-detect backend using declarative delegate_registry.json + intake verb map.
    Coding intent (code/website/html/react) routes to opencode before generic fallback."""
    # Phase 3.6: OpenCLI ("turn any website into a CLI" via logged-in Chrome).
    # Route to the opencli backend when the user explicitly names OpenCLI, or
    # names a known OpenCLI site adapter (facebook/reddit/github/...) in a
    # scrape/use/read context. Keeps it out of the generic hermes/manus paths.
    _OPENCLI_EXPLICIT_RE = re.compile(
        r"\b(opencli|open[- ]?cli|using opencli|via opencli|with opencli)\b", re.I)
    _OPENCLI_SITE_RE = re.compile(
        r"\b(facebook|instagram|twitter|\bx\b|reddit|linkedin|weibo|douyin|"
        r"xiaohongshu|zhihu|tiktok|github|gmail|notion|discord|telegram|wechat|"
        r"slack|amazon|youtube|wikipedia|arxiv|hackernews|spotify|medium|"
        r"producthunt|stackoverflow|steam|imdb|pinterest|substack|devto|"
        r"google|bing|duckduckgo)\b", re.I)
    if _OPENCLI_EXPLICIT_RE.search(task):
        return "opencli"
    # "scrape/read/use <site>" with a known OpenCLI adapter -> opencli backend
    if _OPENCLI_SITE_RE.search(task) and re.search(
            r"\b(scrape|scraping|use|read|get|pull|fetch|summary|search|feed|"
            r"trending|whoami|notifications|post|comment|dm|message|marketplace)\b",
            task, re.I):
        return "opencli"
    # Phase 5: coding heuristic — catches "code a website" even when intake verb misses "code"
    # "VS Code" / "Claude Code" are names, not coding work: "open VS Code"
    # went to the OpenCode coding agent on 2026-09-12.
    _code_text = re.sub(r"\b(?:vs|visual\s+studio|claude)\s+code\b", " ", task, flags=re.I)
    if re.search(r"\b(code|debug|refactor|implement|fix\s+(the\s+)?code|build\s+(a\s+)?(website|site|app|api)|create\s+(a\s+)?(website|web\s*app|react|laravel|python\s+file|html\s+page))\b", _code_text, re.I) or \
       (re.search(r"\b(website|web\s*site|html|react|laravel|python\s+file|javascript|api)\b", _code_text, re.I) and re.search(r"\b(create|make|build|generate|code|implement)\b", _code_text, re.I)):
        return "opencode"
    # Try registry verb_map first (declarative, no hardcoded list)
    # Resolved once: both lookups below read only its verb, and resolving a
    # pasted page is not free.
    intent = None
    reg = _load_delegate_registry()
    if reg:
        verb_map = reg.get("verb_map", {})
        # Use intake to resolve verb
        try:
            from cygnus.brain.intake import resolve_intent, load_corpus
            intent = resolve_intent(task, load_corpus())
            verb = (intent.verb or "").lower()
            handle = verb_map.get(verb)
            if handle:
                # Map handle -> delegate id that handles it (highest priority first)
                delegates = sorted(reg.get("delegates", []), key=lambda d: d.get("priority", 0), reverse=True)
                for d in delegates:
                    if handle in d.get("handles", []) or handle == d.get("id"):
                        be = d.get("id")
                        # Manus guard: media vs code/filesystem -> hermes floor
                        if be == "manus" and re.search(
                                r"\b(code|website|web ?site|html|css|javascript|js|script|api|program|landing page|web ?page|site|web ?app|react|laravel|python file|folder|directory|file|documents?|downloads?|organize|rename|move|copy|verify)\b", task, re.I):
                            return "hermes"
                        return be
                # handle found but no delegate? return handle itself for local dispatch
                if handle in ("music", "desktop", "web", "chatgpt", "manus"):
                    if handle == "manus" and re.search(r"\b(code|website|file|folder|organize)\b", task, re.I):
                        return "hermes"
                    return handle
        except Exception:
            pass
    # Fallback: legacy intake verb_backend (hardcoded list kept for compat)
    try:
        from cygnus.brain.intake import verb_backend, resolve_intent, load_corpus
        intent = intent or resolve_intent(task, load_corpus())
        be = verb_backend(intent.verb)
        if be and be in ("music", "desktop", "web", "manus", "chatgpt", "native"):
            if be == "manus" and re.search(
                    r"\b(code|website|web ?site|html|css|javascript|js|"
                    r"script|api|program|landing page|web ?page|site|"
                    r"web ?app|react|laravel|python file)\b", task, re.I):
                return "hermes"
            if be == "manus" and re.search(
                    r"\b(folder|directory|file|documents?|downloads?|desktop|"
                    r"organize|rename|move|copy|verify)\b", task, re.I):
                return "hermes"
            return be
    except Exception:
        pass
    return "hermes"


# Explicit harness invocation: "create a website USING OPENCODE",
# "search my vault VIA BROWSER-SESSION". When the user names the tool,
# JARVIS must not second-guess - auto-detection is skipped entirely.
_EXPLICIT_HARNESS_RE = re.compile(
    r"\b(?:using|via|with|through|on)\s+(?:the\s+)?"
    r"(opencode|open\s?code|pi|[a-z]+[- ](?:agent|runner|operator|curator|session))\b",
    re.I)

def _explicit_specialist(task: str):
    """Detect an explicit harness/agent mention in the utterance.

    Returns (forced: bool, agent_or_None, cleaned_task). When forced, the
    caller must run the specialist tier and report its result verbatim -
    including failures - because the user chose this tool deliberately.
    """
def _known_agents() -> tuple:
    """Dynamic agent lookup - scans opencode.json's agent map at call time.

    The registry file is the single source of truth: add an agent there and
    JARVIS can route to it with zero code changes. Falls back to an empty
    tuple when the config is missing/unparseable (explicit-name matching
    then simply finds nothing, and auto-routing still works).
    """
    try:
        with open(paths.OPENCODE_JSON, encoding="utf-8") as f:
            agents = json.load(f).get("agent", {})
        return tuple(agents.keys())
    except Exception:
        return ()


# Explicit harness invocation: "create a website USING OPENCODE",
# "search my vault VIA BROWSER-SESSION". When the user names the tool,
# JARVIS must not second-guess - auto-detection is skipped entirely.
_EXPLICIT_HARNESS_RE = re.compile(
    r"\b(?:using|via|with|through|on)\s+(?:the\s+)?"
    r"(opencode|open\s?code|pi|[a-z]+[- ](?:agent|runner|operator|curator|session))\b",
    re.I)


def _explicit_specialist(task: str):
    """Detect an explicit harness/agent mention in the utterance.

    Returns (forced: bool, agent_or_None, cleaned_task). When forced, the
    caller must run the specialist tier and report its result verbatim -
    including failures - because the user chose this tool deliberately.
    """
    t = task.lower()
    known = _known_agents()
    # Named agent wins outright ("via project-runner", "with music agent").
    for name in known:
        if name in t or name.replace("-", " ") in t:
            return True, name, task
    m = _EXPLICIT_HARNESS_RE.search(task)
    if m:
        norm = re.sub(r"[\s-]", "", m.group(1))
        if norm == "opencode":
            cleaned = (task[:m.start()] + " " + task[m.end():]).strip(" ,.()")
            # Bare "using opencode": no named agent. Let JARVIS decide which
            # specialist fits the TASK CONTENT (dynamic registry lookup) instead
            # of dropping to opencode's built-in default agent.
            return True, _pick_specialist(cleaned), (cleaned or task)
        if norm == "pi":
            cleaned = (task[:m.start()] + " " + task[m.end():]).strip(" ,.()")
            # SPIKE: 'using pi' / 'via pi' routes to the sandboxed Pi backend.
            return True, "pi", (cleaned or task)
    return False, None, task


# ---------------------------------------------------------------------------
# External CLI agent tier (Phase 16.5): gemini-cli / codex / claude.
# Data-driven via cli_agents.json; binaries resolved at CALL TIME so
# installing a CLI later needs zero code changes. "using gemini" / "via
# chatgpt" forces this tier with NO Hermes fallback (user's rule).
_CLI_AGENTS_PATH = str(paths.CLI_AGENTS)


def _cli_registry() -> dict:
    try:
        with open(_CLI_AGENTS_PATH, encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except Exception:
        return {}


# What follows a helper's name when the user hands it work ("have Claude fix").
_HANDOFF_VERBS = (r"(?:do|build|make|create|fix|write|code|handle|look|check|review|update|add|"
                  r"change|set|work|tidy|clean|refactor|debug|implement|align|redesign|test|"
                  r"run|finish|repair|patch|edit|improve)\b")


def _explicit_cli_agent(task: str):
    """Detect 'using/via/with <cli-name>' for an external CLI tier.

    Returns (forced: bool, name_or_None, cleaned_task). Only matches when the
    user explicitly names the CLI — auto-detection NEVER sends work here
    (Hermes stays the default executor).
    """
    # "using/via claude", and the ways people hand work over: "delegate this to
    # Claude", "hand it off to Claude", "have Claude Code fix ...". "ask Claude"
    # is left to ask_ai (it may mean the website), and "I have claude
    # installed" does not count: "have X" needs a verb after it. "delegate
    # claude to fix ..." (no "to" before the name) went to Hermes on
    # 2026-09-13, which only described the handoff.
    # "..., use claude" (2026-09-13) and "ask/tell claude to fix ..." and
    # "Claude, fix ..." hand work over too; "I use claude" and "ask claude
    # what ..." do not.
    m = re.search(
        r"\b(?:(?:(?<!\bi\s)(?<!\bwe\s)(?<!\bthey\s)(?<!\bdo\syou\s)(?<!\bdid\syou\s)use|"
        r"using|via|with|through|delegate(?:\s+(?:this|it|that))?(?:\s+to)?|"
        r"hand\s+(?:(?:this|it|that)\s+)?(?:off\s+)?to|give\s+(?:this|it|that)\s+to)\s+(?:the\s+)?"
        r"(?P<a>gemini(?:\s*cli)?|chatgpt(?:\s*cli)?|codex|claude(?:\s*code)?)\b"
        # "claude code" is one name: backtracking must not read its "code"
        # as the verb ("ask claude code what time is it" is a question).
        r"|(?:have|let|get|ask|tell)\s+(?P<b>claude(?:\s*code\b|(?!\s*code\b))|gemini(?:\s*cli)?|codex)"
        r"\s+(?:to\s+)?"
        rf"(?={_HANDOFF_VERBS})"
        r"|^\s*(?:hey\s+)?(?P<c>claude(?:\s*code\b|(?!\s*code\b)))\s*[,:]\s*"
        rf"(?={_HANDOFF_VERBS}))", task, re.I)
    if not m:
        return False, None, task
    spoken = re.sub(r"\s+", " ", (m.group("a") or m.group("b") or m.group("c")).lower().strip())
    reg = _cli_registry()
    for key in reg:
        if spoken == key or spoken == f"{key} cli" or \
           (key == "claude" and spoken == "claude code"):
            tail = re.sub(r"^\s*to\s+", " ", task[m.end():], flags=re.I)
            cleaned = re.sub(r"\s+", " ", task[:m.start()] + " " + tail).strip(" ,.()")
            # "okay jarvis, delegate this to claude: fix the mic" handed Claude
            # "okay jarvis, fix the mic" (2026-09-24). The lead-in is not the task.
            from cygnus.tools.phrases import strip_correction_prefix, strip_fillers
            cleaned = strip_fillers(strip_correction_prefix(cleaned.strip(" ,.:;()"))).strip(" ,.:;()")
            return True, key, (cleaned or task)
    return False, None, task


def _run_cli_agent(task: str, name: str, timeout: int = 300) -> str:
    """Run a one-shot prompt through an external CLI agent with per-delegate brief (Phase 4/5)."""
    import shutil as _shutil
    import subprocess
    spec = _cli_registry().get(name)
    if not spec:
        return f"[cli:{name}] not in cli_agents.json."
    # Phase 4: brief for ideation delegates
    try:
        from cygnus.brain import context_assembler as ca
        brief = ca.assemble_brief(task, delegate=name)
        task = brief + task
    except Exception:
        pass
    launch = _direct_launcher(spec.get("bin", ""))
    if launch is None:
        return (f"[cli:{name}] '{spec.get('bin')}' is not installed on PATH, sir. "
                f"Install it and I'll route to it with no other changes.")
    cmd = list(launch)
    flag = spec.get("prompt_flag")
    if flag:
        cmd.append(flag)
    cmd.append(task)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout,
                              cwd=str(paths.ROOT))
    except subprocess.TimeoutExpired:
        return f"[cli:{name}] timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    if not out and proc.returncode != 0:
        err = (proc.stderr or "").strip()
        # npm .cmd shims often print usage noise; surface the real error only.
        return f"[cli:{name}] failed ({proc.returncode}): {err[:300] or 'no output'}"
    return out.strip() or "(empty response)"


# A named CLI agent (Claude Code, Gemini CLI) runs as a background job, like
# Hermes: the turn answers at once, the Active Tasks card follows the work,
# and the result is announced. It used to run inside the voice turn for
# minutes, logged as a job only after it ended, and "delegate this to Claude"
# never reached it at all: on 2026-09-12 it went to Hermes.
try:                     # .env counts however tools is imported (config loads it too,
    from dotenv import load_dotenv as _load_dotenv    # but only when imported first)
    _load_dotenv()
except Exception:
    pass
CLI_JOB_TIMEOUT = int(os.getenv("JARVIS_CLI_JOB_TIMEOUT", "900"))
_REPO_DIR = str(paths.ROOT)

# Words that are not a task: "test" became a Hermes job waiting for confirm.
_NOT_A_TASK_RE = re.compile(
    r"(?:test(?:ing)?|hi|hello|hey|yo|ok(?:ay)?|yes|yeah|yep|no|nope|sure|thanks?|"
    r"thank\s+you|hmm+|huh|what|nothing|never\s*mind|cygnus|jarvis)[\s.!?,]*", re.I)


def _is_not_a_task(task: str) -> bool:
    # Trailing "?" / "." off first: "Cygnus?" left a lone "?" after the name
    # was stripped as a lead-in, so a check-in waited on a model.
    t = re.sub(r"[\s.!?,]+$", "", (task or "").strip())
    return bool(t) and bool(_NOT_A_TASK_RE.fullmatch(strip_fillers(t).strip() or t))


def _not_a_task_reply(task: str) -> str:
    return f"“{(task or '').strip()}” isn't something I can run as a task. What would you like me to do?"


def _cli_label(name: str) -> str:
    return (_cli_registry().get(name) or {}).get("label") or name


def _cli_installed(name: str) -> bool:
    spec = _cli_registry().get(name) or {}
    return bool(_direct_launcher(spec.get("bin", "")))


def _cli_command(task: str, name: str) -> list | None:
    """The command line for CLI agent `name`, or None when it isn't installed."""
    spec = _cli_registry().get(name) or {}
    launch = _direct_launcher(spec.get("bin", ""))
    if not launch:
        return None
    args = [os.path.expanduser(a) for a in spec.get("args") or []]
    flag = [spec["prompt_flag"]] if spec.get("prompt_flag") else []
    return launch + args + flag + [task]


# "... and open it in Chrome so I can see its output": the request asks to
# see what the job makes. Only then does Cygnus open it afterwards.
_OPEN_ASKED_RE = re.compile(
    r"\b(open|show|display|pull\s+up)\b.{0,40}?\b(chrome|browser|brave|edge|firefox)\b"
    r"|\b(open|show)\s+(it|them)\s+(to\s+me|for\s+me)\b", re.I)
_RESULT_FILE_RE = re.compile(
    r"`([^`\n]+?\.(?:html?|svg|png|jpe?g|pdf))`"
    r"|([A-Za-z]:\\[^\s`'\"<>|*?]+?\.(?:html?|svg|png|jpe?g|pdf))\b"
    r"|((?<![\w\\/:.-])[\w.-]+(?:[\\/][\w.-]+)*\.(?:html?|svg|png|jpe?g|pdf))\b", re.I)


def _result_file(result: str) -> str | None:
    """The file a job's answer names that exists, pages before pictures:
    `running_panel_mockup.html`, playwright_shots\\x.png, C:\\...\\page.html."""
    found = []
    for m in _RESULT_FILE_RE.finditer(result or ""):
        name = next(g for g in m.groups() if g).strip().strip("'\"")
        path = name if os.path.isabs(name) else os.path.join(_REPO_DIR, name)
        if os.path.isfile(path):
            found.append(os.path.abspath(path))
    pages = [p for p in found if p.lower().endswith((".html", ".htm"))]
    return (pages or found or [None])[0]


def open_after_job(asked: str, result: str) -> str:
    """Open the file a finished job made, when the request asked to see it.
    The agent can't: `claude -p` runs with nobody to approve starting a
    program, and on 2026-09-14 all three of Claude's tries to open Chrome
    were refused. Returns what to add to the answer; '' when not asked."""
    m = _OPEN_ASKED_RE.search(asked or "")
    if not m:
        return ""
    path = _result_file(result)
    if not path:
        return "You asked to see it, but the answer names no file I could open."
    which = (re.search(r"\b(chrome|brave|edge|firefox)\b", m.group(0), re.I) or [None, "chrome"])[1].lower()
    exe = None
    if which == "chrome":
        try:
            from cygnus.browser import agent as browser_agent
            exe = browser_agent._chrome_exe()
        except Exception:
            exe = None
    else:
        exe = (_load_app_registry().get(which) or {}).get("bin")
    try:
        if exe and os.path.isfile(exe):
            subprocess.Popen([exe, path], close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            where = _browser_label(which) if which != "chrome" else "Chrome"
        else:
            os.startfile(path)
            where = "your default browser"
    except Exception as e:
        return f"I couldn't open {os.path.basename(path)}: {e}"
    _audit_log("open_after_job", path, "opened", extra={"browser": which})
    return f"I've opened {os.path.basename(path)} in {where} for you."


def _start_cli_job(task: str, name: str, raw_task: str = None, on_done=None,
                   jid: str = None, timeout: int = CLI_JOB_TIMEOUT) -> str:
    """Run `task` through CLI agent `name` in the background; returns the ack.
    `jid`: the job that waited for the confirm, so the same card carries on."""
    from cygnus.jobs import registry as jobreg
    label = _cli_label(name)
    asked = f"{raw_task or ''}\n{task}"          # "... and open it in Chrome" is read at the end
    if not _cli_installed(name):
        if jid:
            jobreg.update(jid, state="error", error=f"{label} is not installed")
        return f"[Error] {label} is not installed on this computer, so I can't hand it this."
    jid = jid or jobreg.create((raw_task or task)[:200], tier="cli", agent=label, background=True)
    jobreg.update(jid, state="running", note="writing the brief")
    _audit_log(f"delegate:{name}", (raw_task or task)[:200], "started", extra={"jid": jid})

    def _fail(result: str) -> None:
        jobreg.update(jid, state="error", error=result[:500], summary=result[:200])
        if on_done:
            try:
                on_done(result)
            except Exception as e:
                print(f"[CLI] on_done raised: {e}", flush=True)

    def _watch():
        # The brief is written here, off the turn: the task restated by a
        # model (15-30 s) and put first, the words as spoken under it, the
        # background last (context_assembler.compose_prompt, 2026-09-24).
        try:
            from cygnus.brain import context_assembler as ca
            prompt = ca.compose_prompt(task, delegate=name, raw_task=raw_task)
        except Exception as e:
            print(f"[CLI] brief failed ({e}); sending the task as given", flush=True)
            prompt = task
        cmd = _cli_command(prompt, name)
        if cmd is None:
            return _fail(f"[Error] {label} is not installed on this computer, so I can't hand it this.")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", cwd=_REPO_DIR)
        except Exception as e:
            return _fail(f"[Error] I couldn't start {label}: {e}"[:300])
        jobreg.update(jid, state="running", note=f"handed to {label}")
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            result = f"[Error] {label} did not finish within {timeout}s."
            jobreg.update(jid, state="error", error=result, summary=result)
        else:
            out = (out or "").strip()
            if proc.returncode != 0 and not out:
                result = f"[Error] {label} stopped: {(err or '').strip()[:300] or 'no output'}"
                jobreg.update(jid, state="error", error=result[:500], summary=result[:200])
            else:
                result = out or f"{label} finished and had nothing to add."
                jobreg.update(jid, state="done", result=result[-6000:], summary=result[:200])
                opened = open_after_job(asked, result)
                if opened:
                    result = f"{result}\n\n{opened}"
                    jobreg.update(jid, note=opened)
        if on_done:
            try:
                on_done(result)
            except Exception as e:
                print(f"[CLI] on_done raised: {e}", flush=True)

    threading.Thread(target=_watch, daemon=True, name=f"cli-{name}-{jid}").start()
    return f"⟳ HERMES_BACKGROUND: On it, sir. {label} is working on it now. (job {jid})"


def _cli_delegate(task: str, name: str, raw_task: str = None, confirm: bool = False,
                  on_done=None) -> str:
    """Hand `task` to CLI agent `name`: at once when it only looks, after the
    user's confirm when it may change files. The confirm uses the Hermes latch,
    so "confirm" / "no" and the brain's confirm handling work unchanged."""
    global _PENDING_HERMES_CALL
    from cygnus.jobs import registry as jobreg
    raw = (raw_task or task).strip()
    if _is_not_a_task(task):
        return _not_a_task_reply(raw)
    # The model's confirm=true releases only the confirm the user just heard
    # for this very task. On its own it started Claude (acceptEdits) unasked.
    pending = _PENDING_HERMES_CALL or {}
    if confirm and pending.get("cli") == name and pending.get("task") == task:
        return confirm_pending(on_done=on_done)
    if _is_safe_task(raw):
        return _start_cli_job(task, name, raw_task=raw, on_done=on_done)
    label = _cli_label(name)
    jid = jobreg.create(raw[:200], tier="cli", agent=label, background=True)
    _PENDING_DESTRUCTIVE["task"] = task
    _PENDING_HERMES_CALL = dict(cli=name, task=task, raw_task=raw, on_done=on_done,
                                jid=jid, ts=time.time())
    jobreg.update(jid, state="waiting-on-confirm", note="needs confirm — say 'confirm' to run",
                  summary=f"Waiting for confirm: {raw[:120]}")
    _audit_log(f"delegate:{name}", raw[:200], "NEEDS_CONFIRM", extra={"jid": jid})
    return (f"[NEEDS_CONFIRM:{jid}] {label} may change files for this, so I'm asking first. "
            f"Say 'confirm' and I'll run it through {label}: \"{raw}\" (job {jid})")


def _specialist_delegate(task: str, agent: str | None, raw_task: str = None, confirm: bool = False,
                         pi: bool = False, timeout: int = 300, background: bool = False,
                         on_done=None) -> str:
    """Hand `task` to an OpenCode specialist (or the Pi sandbox) the way
    _cli_delegate hands work to Claude: at once when it only looks, after the
    user's confirm when it may change things. Before 2026-09-13 these ran with
    no confirm at all."""
    global _PENDING_HERMES_CALL
    from cygnus.jobs import registry as jobreg
    raw = (raw_task or task).strip()
    pending = _PENDING_HERMES_CALL or {}
    if confirm and pending.get("spec") and pending.get("task") == task:
        return confirm_pending(on_done=on_done, background=background)
    if _is_safe_task(raw):
        return _run_specialist_now(task, agent, pi, timeout, background, on_done)
    label = "Pi" if pi else (agent or "OpenCode")
    jid = jobreg.create(raw[:200], tier="specialist", agent=agent, background=True)
    _PENDING_DESTRUCTIVE["task"] = task
    _PENDING_HERMES_CALL = dict(spec=agent or "opencode", pi=pi, task=task, raw_task=raw,
                                timeout=timeout, background=background, on_done=on_done,
                                jid=jid, ts=time.time())
    jobreg.update(jid, state="waiting-on-confirm", note="needs confirm — say 'confirm' to run",
                  summary=f"Waiting for confirm: {raw[:120]}")
    _audit_log(f"delegate:{label}", raw[:200], "NEEDS_CONFIRM", extra={"jid": jid})
    return (f"[NEEDS_CONFIRM:{jid}] {label} may change things for this, so I'm asking first. "
            f"Say 'confirm' and I'll run it through {label}: \"{raw}\" (job {jid})")


def _run_specialist_now(task, agent, pi, timeout, background, on_done) -> str:
    """Run a specialist: in the background (its ack) or in the turn (its reply)."""
    if background:
        run = _run_pi_specialist_bg if pi else _run_specialist_bg
        return run(task, agent, timeout=timeout, on_done=on_done)[0]
    return (_run_pi_specialist if pi else _run_specialist)(task, agent, timeout=timeout)


def _run_specialist(task: str, agent: str | None = None, timeout: int = 300) -> str:
    """Run a task through the OpenCode specialist tier with per-delegate brief (Phase 4)."""
    import shutil; from cygnus.jobs import registry as jobreg
    import subprocess
    oc = _direct_launcher("opencode")
    if oc is None:
        return "[specialist] OpenCode not installed; route to hermes."
    try:
        from cygnus.brain import context_assembler as ca
        brief = ca.assemble_brief(task, delegate=agent or "opencode")
        task = brief + task
    except Exception:
        pass
    # Phase 2: registry — sync specialist also tracked as job for proactive reporting
    jid = jobreg.create(task[:200], tier="specialist", agent=agent, background=False)
    jobreg.update(jid, state="running", note=f"sync specialist {agent or 'build'}")
    cmd = oc + ["run"]
    if agent:
        cmd += ["--agent", agent]
    cmd += _specialist_model_args(agent)      # a working model when its own is down
    cmd.append(task)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=str(paths.ROOT))
    except subprocess.TimeoutExpired:
        return f"[specialist:{agent or 'build'}] timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or 'unknown error')[:300]
        jobreg.update(jid, state="error", error=err, summary=err[:120])
        return f"[specialist] failed: {err}"
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith(">")]
    body = "\n".join(lines).strip() or "(empty response)"
    # Phase 6: check if delegate needs clarification (missing context)
    try:
        from cygnus.brain import context_assembler as ca
        if ca.delegate_needs_clarification(body):
            jobreg.update(jid, state="waiting-on-confirm", note="delegate needs clarification", summary=body[:120])
            return body
    except Exception:
        pass
    jobreg.update(jid, state="done", result=body[-4000:], summary=body.splitlines()[-1][:200] if body else "done")
    return body


# ---------------------------------------------------------------------------
# Specialist grounding + background execution (async specialist tier)
# ---------------------------------------------------------------------------

def _specialist_context(task: str, max_hits: int = 3) -> str:
    """Assemble a vault-grounding block for a specialist brief.

    Queries the Second Brain (semantic when available, keyword fallback) for
    content matching the task and returns a context block to prepend. The
    block LEAVES the orchestrator at spawn — the worker quotes provided facts
    instead of inventing them; the orchestrator keeps nothing.
    """
    try:
        result = execute_tool("search_vault_semantic",
                              {"query": task, "limit": max_hits})
        if not result or "[Error" in result[:20] or "no results" in result.lower():
            raise ValueError(result)
    except Exception:
        try:
            result = execute_tool("search_vault", {"query": task, "limit": max_hits})
        except Exception:
            return ""
    if not result or "[Error" in result[:20] or "no results" in result.lower():
        return ""
    # Trim to a budget so briefs stay lean.
    text = result.strip()
    if len(text) > 2400:
        text = text[:2400] + "\n…(truncated)"
    return ("[CONTEXT FROM SECOND BRAIN - ground ONLY in these facts; "
            "if something needed is missing here, say so plainly]\n"
            + text + "\n[END CONTEXT]\n\n")


_BG_SPECIALISTS: dict[str, dict] = {}   # jid -> {proc, agent}
_BG_LOCK = threading.Lock()


def _run_specialist_bg(task: str, agent: str | None, timeout: int,
                       on_done=None) -> tuple[str, str]:
    """Spawn a specialist as a non-blocking process; returns (ack, jid). Phase 4 brief via context_assembler."""
    import shutil
    import subprocess
    from cygnus.jobs import registry as jobreg
    oc = _direct_launcher("opencode")
    if oc is None:
        return ("[specialist] OpenCode not installed; route to hermes.", "")
    # Phase 4: per-delegate brief (opencode gets code-focused vault slice)
    try:
        from cygnus.brain import context_assembler as ca
        brief = ca.assemble_brief(task, delegate=agent or "opencode")
        full_task = brief + task
    except Exception:
        grounded = _specialist_context(task)
        full_task = (grounded + task) if grounded else task

    jid = jobreg.create(task, tier="specialist", agent=agent, background=True)
    cmd = oc + ["run"]
    if agent:
        cmd += ["--agent", agent]
    cmd += _specialist_model_args(agent)      # a working model when its own is down
    cmd.append(full_task)

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(paths.ROOT))
    except Exception as e:
        jobreg.update(jid, state="error", error=str(e)[:200])
        return f"[specialist] failed to launch: {e}", jid

    with _BG_LOCK:
        _BG_SPECIALISTS[jid] = {"proc": proc, "agent": agent}

    def _watch():
        import threading as _t  # noqa: F401 (clarity)
        try:
            out, err = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = "", ""
            summary = (out or "").strip().splitlines()[-1][:200] if (out or "").strip() \
                else f"timed out after {timeout}s"
            jobreg.update(jid, state="timeout", error=f"timed out after {timeout}s",
                          summary=summary)
            if on_done:
                on_done(f"[specialist:{agent or 'build'}] timed out after {timeout}s.")
            with _BG_LOCK:
                _BG_SPECIALISTS.pop(jid, None)
            return
        finally:
            with _BG_LOCK:
                _BG_SPECIALISTS.pop(jid, None)

        stdout = (out or "").strip()
        # strip opencode's "> agent · model" header line
        body = "\n".join(l for l in stdout.splitlines()
                         if l.strip() and not l.startswith(">")).strip()
        if rc == 0 and body:
            summary = body.splitlines()[-1][:200]
            jobreg.update(jid, state="done", result=body[-4000:], summary=summary)
            if on_done:
                on_done(body)
        else:
            reason = ((err or "").strip() or "non-zero exit")[-300:]
            jobreg.update(jid, state="error", error=reason,
                          summary=summary if False else (body.splitlines()[-1][:200] if body else "failed"))
            if on_done:
                on_done(f"[specialist:{agent or 'build'}] failed: {reason}")

    threading.Thread(target=_watch, daemon=True,
                     name=f"spec-watcher-{jid}").start()
    jobreg.update(jid, state="running",
                  note=f"dispatched to {agent or 'default build agent'}")
    label = agent or "build agent"
    ack = (f"Working on it in the background, sir — {label} has the task. "
           "I'll speak up when it's done.")
    return f"⟳ SPECIALIST_BACKGROUND:{ack}", jid


# ---------------------------------------------------------------------------
# Pi coding-agent specialist tier (SPIKE) — sandboxed Docker backend.
# Parallel to OpenCode; never touches the OpenCode path. Pi has NO built-in
# permission system, so scripts/pi_sandbox.sh runs it inside Docker — the
# container IS the security boundary. Model routing stays on 9Router
# (ag/claude-sonnet-4-6) via the models.json mounted by the wrapper.
# ---------------------------------------------------------------------------

def _run_pi_specialist(task: str, agent: str | None = None, timeout: int = 300) -> str:
    """Run a coding task through the Pi specialist tier (SPIKE, sandboxed).

    Mirrors _run_specialist but invokes scripts/pi_sandbox.sh, which runs Pi
    inside Docker. OpenCode path is untouched.
    """
    import shutil, subprocess; from cygnus.jobs import registry as jobreg
    bash = shutil.which("bash")
    if bash is None:
        return "[pi] bash unavailable; cannot launch sandbox."
    script = str(paths.SCRIPTS_DIR / "pi_sandbox.sh")
    if not os.path.exists(script):
        return "[pi] sandbox wrapper missing; route to opencode."
    try:
        from cygnus.brain import context_assembler as ca
        brief = ca.assemble_brief(task, delegate="pi")
        task = brief + task
    except Exception:
        pass
    jid = jobreg.create(task[:200], tier="pi", agent=agent or "pi", background=False)
    jobreg.update(jid, state="running", note="sandboxed pi coding task")
    # repo to mount = current working dir (JARVIS cwd) by default
    repo = str(paths.ROOT)
    cmd = [bash, script, repo, task, str(timeout)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[pi] timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "unknown error")[:300]
        jobreg.update(jid, state="error", error=err, summary=err[:120])
        return f"[pi] failed: {err}"
    body = out or "(empty response)"
    try:
        from cygnus.brain import context_assembler as ca
        if ca.delegate_needs_clarification(body):
            jobreg.update(jid, state="waiting-on-confirm", note="pi needs clarification")
            return body
    except Exception:
        pass
    jobreg.update(jid, state="done", result=body[-4000:],
                  summary=body.splitlines()[-1][:200] if body else "done")
    return body


_BG_PI: dict = {}
_PI_LOCK = threading.Lock()


def _run_pi_specialist_bg(task: str, agent: str | None, timeout: int,
                           on_done=None) -> tuple[str, str]:
    """Spawn a Pi specialist as a non-blocking process; returns (ack, jid)."""
    import shutil, subprocess; from cygnus.jobs import registry as jobreg
    script = str(paths.SCRIPTS_DIR / "pi_sandbox.sh")
    if not os.path.exists(script):
        return ("[pi] sandbox wrapper missing; route to opencode.", "")
    try:
        from cygnus.brain import context_assembler as ca
        brief = ca.assemble_brief(task, delegate="pi")
        full_task = brief + task
    except Exception:
        full_task = task
    jid = jobreg.create(task, tier="pi", agent=agent or "pi", background=True)
    repo = str(paths.ROOT)
    cmd = ["bash", script, repo, full_task, str(timeout)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        jobreg.update(jid, state="error", error=str(e)[:200])
        return f"[pi] failed to launch: {e}", jid
    with _PI_LOCK:
        _BG_PI[jid] = {"proc": proc, "agent": agent}

    def _watch():
        try:
            out, err = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = "", ""
            jobreg.update(jid, state="timeout", error=f"timed out after {timeout}s")
            with _PI_LOCK:
                _BG_PI.pop(jid, None)
            if on_done:
                on_done(f"[pi] timed out after {timeout}s.")
            return
        finally:
            with _PI_LOCK:
                _BG_PI.pop(jid, None)
        stdout = (out or "").strip()
        if rc == 0 and stdout:
            jobreg.update(jid, state="done", result=stdout[-4000:],
                          summary=stdout.splitlines()[-1][:200])
            if on_done:
                on_done(stdout)
        else:
            reason = ((err or "").strip() or "non-zero exit")[-300:]
            jobreg.update(jid, state="error", error=reason, summary=reason[:120])
            if on_done:
                on_done(f"[pi] failed: {reason}")

    threading.Thread(target=_watch, daemon=True,
                     name=f"pi-watcher-{jid}").start()
    jobreg.update(jid, state="running", note="dispatched to pi (sandboxed)")
    ack = ("Working on it in the background, sir — Pi (sandboxed) has the task. "
           "I'll speak up when it's done.")
    return f"⟳ PI_BACKGROUND:{ack}", jid


def run_autonomous(goal: str, timeout: int = 1800, confirm: bool = False) -> str:
    """Phase 15: launch a supervised autonomous Hermes job — allowlist-gated (Phase 1)."""
    from cygnus.jobs import autonomous
    goal = str(goal or "").strip()
    if not goal:
        return "[Error] No goal given for autonomous run."
    if _is_not_a_task(goal):
        return _not_a_task_reply(goal)
    # A goal that names a helper ("delegate this to Claude") goes to it, not to
    # Hermes' autonomous runner, which the model picked for it on 2026-09-12.
    forced, name, cleaned = _explicit_cli_agent(goal)
    if forced:
        return _cli_delegate(cleaned, name, raw_task=goal, confirm=confirm)
    # A goal that may change things waits for the user's confirm. Asking for
    # the same goal again is not one: before 2026-09-13 the model calling this
    # twice in a turn launched it unasked.
    if not _is_safe_task(goal):
        if confirm and _PENDING_DESTRUCTIVE.get("autonomous") == goal:
            return confirm_autonomous()
        _PENDING_DESTRUCTIVE["autonomous"] = goal
        _PENDING_DESTRUCTIVE["autonomous_ts"] = time.time()
        _PENDING_DESTRUCTIVE["autonomous_timeout"] = timeout
        _audit_log("run_autonomous", goal, "NEEDS_CONFIRM")
        return ("[NEEDS_CONFIRM] That goal could change state on this machine and isn't on the safe allowlist. "
                f"Say 'confirm' to let me run it autonomously: \"{goal}\"")
    return _start_autonomous(goal, timeout)


def confirm_autonomous() -> str | None:
    """Run the goal waiting for the user's confirm; None when none is."""
    _expire_confirms()
    goal = _PENDING_DESTRUCTIVE.get("autonomous")
    if not goal:
        return None
    timeout = _PENDING_DESTRUCTIVE.pop("autonomous_timeout", None) or 1800
    _PENDING_DESTRUCTIVE["autonomous"] = None
    _PENDING_DESTRUCTIVE["autonomous_ts"] = 0.0
    _audit_log("run_autonomous", goal, "confirmed")
    return _start_autonomous(goal, timeout)


def _start_autonomous(goal: str, timeout: int) -> str:
    from cygnus.jobs import autonomous
    ack, jid = autonomous.start_goal(goal, timeout=timeout,
                                     progress_cb=getattr(_tools_tls, "cb", None))
    _audit_log("run_autonomous", goal, "queued", extra={"jid": jid})
    return ack


def job_control(action: str, jid: str | None = None) -> str:
    """Phase 15/16: status query / stop for autonomous jobs."""
    from cygnus.jobs import autonomous
    action = str(action or "").strip().lower()
    if action == "stop":
        # Voice path ("stop the task") arrives WITHOUT an id — resolve the
        # single active job instead of failing on None.
        if jid is None:
            js = autonomous.active_jobs()
            jid = js[0] if js else None
        return (autonomous.cancel(jid) and "Stopped, sir."
                or "[Error] No running autonomous job to stop.")
    if action == "status":
        # No id: every kind of task (Hermes hand-offs too, which the autonomous
        # status file never saw), in plain words.
        if jid is None:
            from cygnus.brain import plain_reply
            return plain_reply.job_status()
        return autonomous.latest_status(jid)
    return f"[Error] Unknown job_control action '{action}' (use status|stop)."


# Moved to memory.py (restructure phase 5); re-exported so
# `tools.<name>` keeps working for callers and tests.
from cygnus.tools.memory import (  # noqa: E402,F401
    _get_embedding_model_wrapper,
    _get_honcho,
    _HONCHO_CLIENT,
    _HONCHO_PEER,
    _HONCHO_WORKSPACE,
    _PROFILE_PATH,
    _REMEMBER_CAP,
    _REMEMBER_HEADING,
    _router_audn_classifier,
    get_memory_context,
    remember_fact,
    remember_in_honcho,
)


_SPECIALIST_ROUTES = (
    # (regex on lowercase task, opencode agent name) - first match wins.
    (r"\b(vault|second brain|obsidian notes?)\b", "browser-session"),
    (r"\b(chatgpt|chat gpt)\b.*\b(history|search|past)\b", "browser-session"),
    (r"\b(dev server|irims|portfolio project|start .* project)\b", "project-runner"),
    (r"\b(generate|create|make)\b.*\b(image|video|logo|thumbnail|document)\b", "media-creator"),
)


def _specialist_model_args(agent: str | None) -> list:
    """["-m", "ninerouter/<model>"] when the OpenCode agent's own 9router model
    is not answering (router_health) and another is. OpenCode's agents are set
    to cx/gpt-5.4-mini (no key) and antigravity models (out of quota), so on
    2026-09-12 every hand-off failed with "Missing API key"."""
    try:
        import importlib as _il
        router_health = _il.import_module("cygnus.brain.router_health")   # honours sys.modules (tests swap in a stand-in)
        healthy = router_health.healthy_models()
        if not healthy:
            return []
        with open(paths.OPENCODE_JSON,
                  encoding="utf-8") as f:
            cfg = json.load(f)
        model = (((cfg.get("agent") or {}).get(agent or "") or {}).get("model")
                 or cfg.get("model") or "")
        # OpenCode refuses a 9router model its config does not list.
        listed = set(((cfg.get("provider") or {}).get("ninerouter") or {}).get("models") or {})
    except Exception:
        return []
    provider, _, name = model.partition("/")
    usable = [m for m in healthy if m in listed]
    if provider != "ninerouter" or name in healthy or not usable:
        return []
    return ["-m", f"ninerouter/{usable[0]}"]


def _pick_specialist(task: str) -> str | None:
    """Choose a specialist agent for tasks the fast-path can't handle.
    Returns None when nothing matches strongly enough (→ hermes keeps it)."""
    t = task.lower()
    for pat, agent in _SPECIALIST_ROUTES:
        if re.search(pat, t):
            return agent
    return None


# Local / direct backends (no Hermes spawn). Each returns a string result.
def _dispatch_opencli(task: str, confirm: bool) -> str:
    """Translate a natural phrase into an OpenCLI command, then run_opencli().

    Maps common phrases to the right `opencli <site> <subcommand>` form and
    forwards the confirm gate. This is the bridge from delegate() -> run_opencli.
    """
    t = task.strip()
    low = t.lower()

    # Explicit "opencli <site> <subcommand>" already well-formed -> pass through.
    m = re.match(r"^open[- ]?cli\s+(.+)$", low)
    if m:
        return run_opencli(m.group(1).strip(), confirm=confirm)

    # site + verb -> opencli <site> <sub>
    SITE = {
        "facebook": "facebook", "fb": "facebook", "instagram": "instagram",
        "ig": "instagram", "twitter": "twitter", "x": "twitter",
        "reddit": "reddit", "linkedin": "linkedin", "github": "github",
        "youtube": "youtube", "yt": "youtube", "wikipedia": "wikipedia",
        "wiki": "wikipedia", "arxiv": "arxiv", "hackernews": "hackernews",
        "hn": "hackernews", "spotify": "spotify", "medium": "medium",
        "producthunt": "producthunt", "stackoverflow": "stackoverflow",
        "steam": "steam", "imdb": "imdb", "pinterest": "pinterest",
        "substack": "substack", "devto": "devto", "amazon": "amazon",
        "google": "google", "bing": "bing", "duckduckgo": "duckduckgo",
        "tiktok": "tiktok", "zhihu": "zhihu", "douyin": "douyin",
        "xiaohongshu": "xiaohongshu", "weibo": "weibo", "notion": "notion",
        "discord": "discord", "telegram": "telegram", "wechat": "wechat",
        "slack": "slack",
    }
    found_site = None
    for key, val in SITE.items():
        if re.search(rf"\b{re.escape(key)}\b", low):
            found_site = val
            break

    if found_site:
        if "trending" in low:
            sub = "trending"
        elif re.search(r"\b(feed|timeline|home)\b", low):
            sub = "feed"
        elif re.search(r"\b(notif|alert)\b", low):
            sub = "notifications"
        elif re.search(r"\b(friend|friends)\b", low):
            sub = "friends"
        elif re.search(r"\b(group|groups)\b", low):
            sub = "groups"
        elif re.search(r"\b(market|marketplace)\b", low):
            sub = "marketplace-listings"
        elif re.search(r"\b(summar|y|about|info|profile)\b", low):
            sub = "summary" if found_site in ("wikipedia", "arxiv") else "profile"
        elif re.search(r"\b(search|find|scrape|lookup)\b", low):
            sub = "search"
        elif re.search(r"\b(post|publish|send|comment|dm|message|reply|create|upload)\b", low):
            sub = "post" if found_site == "facebook" else "search"
        elif re.search(r"\b(whoami|me|account)\b", low):
            sub = "whoami"
        else:
            # No explicit verb: wikipedia/arxiv default to summary; everything
            # else defaults to feed (most sites have a feed/timeline).
            sub = "summary" if found_site in ("wikipedia", "arxiv") else "feed"
        # Extract a query tail when present (e.g. "...search python" -> query).
        # Strip the site name first so it isn't doubled into the query.
        stripped = re.sub(rf"\b{re.escape(found_site)}\b", "", low).strip()
        q = ""
        qm = re.search(r"(?:search|find|scrape|lookup|summary|about|profile|post|read|for)\s+(?:for\s+|on\s+|about\s+)?(.+)$", stripped)
        if qm and sub in ("search", "summary", "profile", "post"):
            q = qm.group(1).strip()
        cmd = f"{found_site} {sub}".rstrip()
        if q and sub in ("search", "summary", "profile", "post"):
            # Site adapters take the query as a single positional argument; a
            # multi-word query must be quoted so it isn't split into N args.
            q_quoted = f'"{q}"' if " " in q else q
            cmd = f"{found_site} {sub} {q_quoted}"
        return run_opencli(cmd, confirm=confirm)

    # Fallback: hand the whole thing to opencli as a raw command attempt.
    return run_opencli(t, confirm=confirm)


_DESKTOP_VERB_RE = re.compile(
    r"^(?:(?:please|jarvis|cygnus|hey|ok(?:ay)?|now|so|just|i\s+mean|"
    r"(?:can|could|would|will)\s+you)[,\s]+)*(open|launch|start|run|close|quit|exit)\s+(.+)$",
    re.I)


def _desktop_dispatch(task: str):
    """Open or close the app a command names. A question that merely mentions
    an app is not a desktop action: "what is the status of the sticky brain
    on open source repo" opened Sticky Brain (2026-09-11), because the whole
    sentence went to open_application and matched the app inside it. Returns
    None for those, and delegate() hands them to Hermes."""
    m = _DESKTOP_VERB_RE.match((task or "").strip().rstrip(".!?"))
    if not m:
        return None
    tool = "close_application" if m.group(1).lower() in ("close", "quit", "exit") \
        else "open_application"
    target = m.group(2).strip()
    # A registered website is opened as one; "open github" is not an app.
    if tool == "open_application":
        kind, key = resolve_open_target(target)
        if kind == "site":
            return execute_tool("open_site", {"name": key})
    return execute_tool(tool, {"app": target})


_LOCAL_DISPATCH = {
    "music": lambda task: (
        execute_tool("stop_music", {}) if "stop" in task.lower()
        else execute_tool("play_music", {"query": task})
    ),
    "desktop": lambda task: _desktop_dispatch(task),
    "web": lambda task: execute_tool("search_web", {"query": task}),
    "chatgpt": lambda task: execute_tool("ask_chatgpt",
                                         {"prompt": task, "submit": True}),
    "manus": lambda task: __import__("cygnus.browser.manus", fromlist=["_"]).delegate_to_manus(task),
    "opencli": lambda task: _dispatch_opencli(task, confirm),
}


def delegate(
    task: str,
    backend: str | None = None,
    confirm: bool = False,
    grounded: bool = True,
    background: bool = False,
    on_done=None,
    timeout: int = 300,
    max_turns: int = 15,
    progress_cb=None,
) -> str:
    """Unified delegation — the ONE primitive for routing tasks.

    backend: "hermes" | "music" | "desktop" | "web" | None (auto-detect).
    grounded: inject JARVIS memory context (profile, session, vault) — default True.
    confirm: pass True to run a previously-confirmed destructive task.
    background/on_done: async fire-and-forget (Phase 4 voice pattern).
    progress_cb: Phase 4 — stream milestone status ("Delegating to Hermes...").

    Local backends (music/desktop/web) run instantly via execute_tool().
    Hermes backend runs the full agent with confirm gate + optional grounding.
    """
    task = str(task or "").strip()
    if not task:
        return "[Error] No task given."
    # Local routing reads the request without "now / okay / jarvis" lead-ins;
    # logs and non-local backends keep the spoken text.
    _rtask = strip_fillers(task)
    if _is_not_a_task(task):
        return _not_a_task_reply(task)

    # ---- Apps-panel action rules win over the generic search/open paths ----
    _rule_out = try_rule_action(_rtask)
    if _rule_out is not None:
        return _rule_out

    # ---- Phase 9: conversation window (local fast path context) ----------
    try:
        from cygnus.brain import conversation_window as cw
    except Exception:
        cw = None

    if cw is not None:
        kind = cw.classify(_rtask)

        # 1) Answering our own app-vs-site question ("site" / "the app")
        if kind == "clarify_answer":
            pend = cw.pending_clarify()
            choice = cw.answer_clarify(_rtask)
            if pend and choice:
                try:
                    from cygnus.brain import dialogue_state
                    if dialogue_state.pending("app_or_site"):
                        dialogue_state.answered()
                except Exception:
                    pass
                name = pend.get("payload", {}).get("name", "")
                cw.append(task, "clarify_answer")
                result = (open_site(name) if choice == "site"
                          else execute_tool("open_application", {"app": name}))
                cw.append(name, "command",
                          tool=("open_site" if choice == "site"
                                else "open_application"), result=result)
                return result
            # unclear answer -> fall through to normal routing, drop the ask
            cw.append(task, "command")

        # 2) Fragment / follow-up ("also youtube", "again")
        elif kind in ("fragment", "contextual"):
            comp = cw.complete_fragment(re.sub(r"^(and|also|too|then)\s+", "",
                                               _rtask, flags=re.I))
            if comp:
                tag, target = comp
                if tag == "repeat":
                    cw.append(task, "fragment", tool=tag)
                    return f"Repeating: {target or 'previous action'}."
                if tag == "open_target":
                    resolved = resolve_open_target(target)
                    if resolved[0] == "site":
                        cw.append(task, "fragment")
                        result = open_site(resolved[1])
                        cw.append(target, "command", tool="open_site",
                                  result=result)
                        return result
                    if resolved[0] == "app":
                        cw.append(task, "fragment")
                        result = execute_tool("open_application",
                                              {"app": resolved[1]})
                        cw.append(target, "command", tool="open_application",
                                  result=result)
                        return result
                    # fragment named something unknown -> let normal flow try
            cw.append(task, "fragment")

    # ---- Phase 10: search-target routing ---------------------------------
    # "search X in chatgpt" -> ChatGPT history; "... in gemini" -> typed into
    # Gemini; correction ("I mean ...") replaces the previous query.
    if cw is not None:
        try:
            parsed = parse_search_command(_rtask)
        except Exception:
            parsed = None
        if parsed is not None:
            corrected = bool(parsed.get("corrected"))
            prev = cw.last()
            if corrected and prev and prev.get("tool") in ("search_web",
                                                           "execute_search"):
                # replace the previous turn's query with this one
                result = execute_search(parsed)
                cw.append(task, "command", tool="execute_search", result=result)
                return (f"Corrected — {result}" if "[error]" not in result.lower()
                        else result)
            result = execute_search(parsed)
            cw.append(task, "command", tool="execute_search", result=result)
            return result

    # ---- No-target guard: an open/visit that names nothing -----------------
    # "visit the..." went to Hermes as a 15-turn / 300s job with nothing to
    # act on. Ask instead — a bare open-site request never leaves JARVIS.
    # "I mean visit X" is matched as "visit X"; cw logs keep the spoken text.
    _otask = strip_fillers(strip_correction_prefix(_rtask))
    # "Can you open github for me?" is "open github": without this it missed
    # the registry fast path below and opened the app "github for me".
    _asked = _REQUEST_LEAD_RE.match(_otask)
    if _asked and _asked.end() < len(_otask):
        _otask = _otask[_asked.end():].strip()
    _open_m = re.match(r"^\W*(?:(?:please|jarvis|cygnus|hey)\b\W*)*"
                       r"(open|launch|go\s+to|goto|visit|browse|take\s+me\s+to)\b(.*)$",
                       _otask, flags=re.I | re.S)
    if _open_m:
        _rest = re.sub(r"\bfor\s+me\b", " ", _open_m.group(2).lower())
        _words = re.findall(r"[a-z0-9]+(?:[.'][a-z0-9]+)*", _rest)
        if all(w in ("please", "jarvis", "cygnus", "sir", "the", "a", "an", "to", "my",
                     "me", "up", "now") for w in _words):
            return "Which site or app should I open?"

    # ---- Phase 8 fast path: dual-registry "open X" -----------------------
    if re.match(r"^(open|launch|go to|goto|visit)\b", _otask.lower()):
        stripped = re.sub(r"^(open|launch|go to|goto|visit)\s+", "", _otask, flags=re.I)
        # "visit X on Brave" / "visit X on Brave now": the browser is where
        # to open it, not what.
        stripped, _bword = _split_browser(stripped)
        browser8 = _browser_key(_bword) if _bword else None
        kind8, target8 = resolve_open_target(stripped, browser=browser8)
        if kind8 == "site":
            result = open_site(target8, browser=browser8)
            if cw is not None:
                cw.append(task, "command", tool="open_site", result=result)
            return result
        if kind8 == "clarify" and isinstance(target8, dict) and not target8.get("name"):
            # "open it" / "open the same one": open-shaped, nothing named.
            question = "Which one, sir? Name the app or the site."
            if cw is not None:
                cw.append(task, "clarify", payload=dict(target8))
            try:
                from cygnus.brain import dialogue_state
                dialogue_state.ask("open_choice", question, dict(target8), owner="tools")
            except Exception:
                pass
            return question
        if kind8 == "clarify" and isinstance(target8, dict):
            if cw is not None:
                cw.append(task, "clarify", payload={"name": target8["name"]})
            question = (f"Found '{target8['name']}' both as an installed app and a "
                        f"website — which one, the app or the site?")
            try:                             # one place knows every open question
                from cygnus.brain import dialogue_state
                dialogue_state.ask("app_or_site", question, {"name": target8["name"]}, owner="tools")
            except Exception:
                pass
            return question
        if kind8 == "app":
            result = execute_tool("open_application", {"app": target8})
            if cw is not None:
                cw.append(task, "command", tool="open_application",
                          result=result)
            return result
        # A visit always means a website: an unresolved one gets open_site's
        # registry-miss answer (or opens a raw URL), never a Hermes job.
        # So does anything spoken "on <browser>". Not a question, though:
        # "can you visit websites?" asks what JARVIS can do.
        if (_otask.lower().startswith("visit") and not _asked) or browser8:
            result = open_site(re.sub(r"^(?:the|my)\s+", "", stripped, flags=re.I),
                               browser=browser8)
            if cw is not None:
                cw.append(task, "command", tool="open_site", result=result)
            return result
        if cw is not None:
            cw.append(task, "command")

    # ---- Explicit external-CLI invocation ("using gemini", "via chatgpt") ----
    # Runs BEFORE everything else; the user's choice is authoritative and
    # failures are reported VERBATIM — no silent Hermes fallback.
    cli_forced, cli_name, cli_task = _explicit_cli_agent(task)
    if cli_forced:
        if cw is not None:
            cw.append(task, "command", tool=f"cli:{cli_name}")
        return _cli_delegate(cli_task, cli_name, raw_task=task, confirm=confirm, on_done=on_done)

    # ---- Explicit harness invocation (user names the tool) ----------------
    # "create a website using opencode", "via project-runner", ...
    # Runs BEFORE auto-detection; the user's choice is authoritative, so
    # failures are reported verbatim instead of silently falling to Hermes.
    forced, forced_agent, cleaned = _explicit_specialist(task)
    if forced:
        # SPIKE: 'using pi' / 'via pi' routes to the sandboxed Pi backend.
        pi = (forced_agent or "").lower() == "pi"
        return _specialist_delegate(cleaned, None if pi else forced_agent, raw_task=task,
                                    confirm=confirm, pi=pi, timeout=timeout,
                                    background=background, on_done=on_done)

    if backend:                          # the model may say "Claude Code" or "Gemini CLI"
        backend = str(backend).strip().lower()
        backend = {"claude code": "claude", "claude-code": "claude",
                   "gemini cli": "gemini", "gemini-cli": "gemini"}.get(backend, backend) or None
    if backend is None:
        backend = _detect_backend(task)

    # Fast path: local tools (no Hermes spawn) — includes registry-driven handles
    handler = _LOCAL_DISPATCH.get(backend)
    if handler:
        result = handler(task)
        if result is not None:
            # Phase 1 audit: local dispatches also logged
            _audit_log(f"delegate:{backend}", task[:200], "local_dispatch", extra={"backend": backend})
            return result
        backend = "hermes"      # the local handler declined: not its kind of task

    # Phase 5: CLI delegates (claude / gemini / codex), named by the model as
    # the backend: background job, confirm first when files may change.
    if backend in _cli_registry():
        return _cli_delegate(task, backend, raw_task=task, confirm=confirm, on_done=on_done)

    # Specialist tier: opencode or hermes floor with domain match
    if backend == "opencode":
        # Direct opencode delegate (verb code/debug/refactor)
        result = _specialist_delegate(task, None, confirm=confirm, timeout=timeout,
                                      background=background, on_done=on_done)
        if not result.startswith("[specialist]"):
            return result
        # fall through to hermes if opencode failed/unavailable
    if backend == "hermes":
        agent = _pick_specialist(task)
        if agent:
            result = _specialist_delegate(task, agent, confirm=confirm, timeout=timeout,
                                          background=background, on_done=on_done)
            if not result.startswith("[specialist]"):
                return result

    # Hermes path: full agent with confirm gate + grounding (Phase 4 brief)
    if grounded:
        return delegate_to_hermes_grounded(
            task, timeout=timeout, max_turns=max_turns,
            confirm=confirm, background=background, on_done=on_done,
            progress_cb=progress_cb)
    return delegate_to_hermes(
        task, timeout=timeout, max_turns=max_turns,
        confirm=confirm, raw_task=task,
        background=background, on_done=on_done,
        progress_cb=progress_cb)


def write_to_notepad(content: str, filename: str = "") -> str:
    """Put text into a Notepad window and show it to the user.

    Deliberately file-backed rather than keystroke-driven: JARVIS is a voice
    assistant, so a SendKeys approach would type into whatever window happened
    to have focus when the user spoke, mangle any character needing a modifier,
    and silently lose the text if Notepad was slow to appear. Writing the file
    first and handing it to Notepad is atomic, survives a crash, and leaves the
    user with something they can actually save.
    """
    try:
        if filename and str(filename).strip():
            p = _resolve_write_path(str(filename).strip())
            if p.suffix == "":
                p = p.with_suffix(".txt")
        else:
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H%M")
            p = DEFAULT_WRITE_DIR / f"Cygnus Note {stamp}.txt"

        p.parent.mkdir(parents=True, exist_ok=True)
        # Notepad on older Windows builds only renders CRLF correctly.
        text = str(content).replace("\r\n", "\n").replace("\n", "\r\n")
        p.write_text(text, encoding="utf-8")

        if platform.system() == "Windows":
            subprocess.Popen(["notepad.exe", str(p)])
        else:
            os.startfile(str(p))  # pragma: no cover - non-Windows dev only
        return f"Opened Notepad with {len(str(content))} characters, saved as {p}"
    except Exception as e:
        return f"[Error] Could not write to Notepad: {str(e)}"


# Spoken name -> what Windows can actually launch. A user says "open Microsoft
# Word"; ShellExecute needs "winword". Without this the model passes the human
# name through verbatim and every Office app fails to open.
# Phase 14b: data now lives in app_aliases.json (editable without code changes);
# this dict is the loaded cache with a built-in fallback if the file is missing.
_ALIASES_PATH = str(paths.APP_ALIASES)
_APP_ALIASES_FALLBACK = {
    "word": "winword", "microsoft word": "winword", "ms word": "winword",
    "excel": "excel", "powerpoint": "powerpnt", "notepad": "notepad.exe",
    "calculator": "calc", "paint": "mspaint", "explorer": "explorer",
    "cmd": "cmd", "task manager": "taskmgr", "edge": "msedge",
    "chrome": "chrome", "brave": "brave", "settings": "ms-settings:",
}


def _load_aliases() -> dict:
    try:
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            return json.load(f).get("aliases", _APP_ALIASES_FALLBACK)
    except Exception:
        return dict(_APP_ALIASES_FALLBACK)


APP_ALIASES = _load_aliases()


def reload_aliases() -> int:
    """Re-read app_aliases.json (e.g. after a manual edit). Returns entry count."""
    global APP_ALIASES
    APP_ALIASES = _load_aliases()
    return len(APP_ALIASES)


def _product_exe_map():
    """Friendly product phrase -> exe stem, from app_aliases.json."""
    try:
        with open(_ALIASES_PATH, encoding="utf-8") as f:
            return json.load(f).get("product_exe", {})
    except Exception:
        return {"word": "winword", "excel": "excel", "edge": "msedge"}


# Leading verbs/filler the voice model often includes in the app argument.
_APP_VERB_RE = re.compile(
    r"^(?:please\s+)?(?:can you\s+)?"
    r"(?:open|launch|start|run|boot|fire up|bring up|pull up|show me|"
    r"open up|start up)\s+(?:the\s+|my\s+|a\s+)*", re.IGNORECASE)
# Trailing and sentence punctuation; a dot inside a name ("python 3.14",
# "node.js") is part of it.
_APP_TRAIL_RE = re.compile(r"[.\s]+$|[,!?;:]+|\.(?!\w)")


def _clean_app_name(app: str) -> str:
    """Strip command verbs, articles and punctuation from an app request.

    The brain sometimes passes a whole sentence ('Open the Microsoft Word.')
    instead of just the app name; this reduces it to 'Microsoft Word'.
    """
    s = str(app).strip()
    prev = None
    while prev != s:
        prev = s
        s = _APP_VERB_RE.sub("", s).strip()
        s = s.strip('"').strip("'").strip()
        s = _APP_TRAIL_RE.sub("", s).strip()
        s = strip_target_noise(s)
    return s or str(app).strip()


_NEW_WINDOW_RE = re.compile(r"\b(new|another|second|fresh|separate)\b", re.I)


def _asked_for_new(app: str) -> bool:
    """"Open a new PowerPoint": in the app name, or in the turn's own words."""
    import sys as _sys
    if _NEW_WINDOW_RE.search(app or ""):
        return True
    brain = _sys.modules.get("brain_gemini")
    said = getattr(getattr(brain, "_progress_local", None), "user_text", "") if brain else ""
    return bool(re.search(r"\b(new|another|second|fresh|separate)\s+(\w+\s+){0,2}"
                          r"(window|instance|copy|one|document|deck|file)\b"
                          r"|\bopen\s+(a\s+)?(new|another|second)\b", said or "", re.I))


def _note_opened(name, entry=None):
    """Remember the app just opened or shown: "close it" a moment later
    means this one (the bare pronoun used to be refused)."""
    try:
        from cygnus.brain import dialogue_state
        dialogue_state.remember_result("app", app=(entry or {}).get("label") or name,
                                       title=name)
    except Exception:
        pass


def _focus_running(names, binp) -> str | None:
    """Bring the app's open window forward rather than launch it again:
    "show PowerPoint" from the phone opened a second PowerPoint (2026-09-14)."""
    try:
        from cygnus.apps import running as running_apps
        row = running_apps.app_window(names, exe=binp)
        if row:
            _note_opened(row["name"])
            ok, said = running_apps.bring_to_front(row)
            # A window that won't come forward is still open: say so, never
            # launch a second copy.
            return (f"{row['name']} was already open, so I brought it to the front, sir."
                    if ok else f"{row['name']} is already open. {said}")
    except Exception:
        pass
    return None


def open_application(app: str, action: str = None, query: str = None,
                     _rule_hops: int = 0, new_window: bool = False) -> str:
    """Open an application by friendly name, resolving it via the capability manifest.

    Prefers machine_capabilities.resolve() (live scan of PATH / Program Files /
    Start-Menu .lnk) and falls back to APP_ALIASES, then a last-chance .exe strip.
    Non-destructive. For music apps, action="search" opens the in-app search pane.
    """
    import os as _os
    system = platform.system()
    requested = _clean_app_name(app)
    new_window = new_window or _asked_for_new(app)   # carried through the re-entries below

    # 0) Shell folders: "open documents folder" / "open downloads" is a File
    # Explorer navigation, not an app launch. Resolve BEFORE app matching so
    # the registry can never misread 'folder' as a substring app name.
    _n0 = requested.lower().strip()
    _n0 = re.sub(r"\b(folders?|directory|dir)\b", "", _n0).strip()  # drop 'folder'
    _n0 = _n0.rstrip("s").strip()                                    # tolerate plurals
    _SHELL_DIRS = {
        "document": "~/Documents", "download": "~/Downloads",
        "picture": "~/Pictures", "music": "~/Music",
        "video": "~/Videos", "desktop": "~/Desktop",
    }
    if _n0 in _SHELL_DIRS:
        p = _os.path.expanduser(_SHELL_DIRS[_n0])
        if _os.path.isdir(p):
            try:
                _os.startfile(p)          # Windows: opens in File Explorer
            except AttributeError:        # non-Windows fallback
                import subprocess as _sp
                _sp.Popen(["xdg-open" if system == "Linux" else "open", p])
            return f"Opened your {_n0}s folder in File Explorer, sir."
        return f"I couldn't find the {_n0}s folder on this machine, sir."

    target = APP_ALIASES.get(requested.lower(), requested)

    # Phase 8 — opt-in fast path (new router wins). If a REGISTERED+ENABLED
    # app matches, use it directly and skip the blind 853-app scan. Otherwise
    # fall through to the legacy matcher below (old+new run in parallel; the
    # opt-in set is the new router's candidate pool, the scan is the fallback).
    try:
        from cygnus.apps.curate import registered_match, is_enabled
        _reg_key = registered_match(requested)
        # ponytail: skip re-entry when _reg_key == requested (infinite loop).
        # Fix: only re-enter when the key DIFFERS (alias resolution).
        if _reg_key and _reg_key != requested and is_enabled(_reg_key):
            try:
                from cygnus.apps.capabilities import load_registry
                _apps = (load_registry() or {}).get("apps", {})
                _entry = _apps.get(_reg_key)
                if _entry and _entry.get("bin"):
                    # Re-enter with the exact key so the rest of the function
                    # resolves the registered app normally (rules, launch, etc.)
                    return open_application(_reg_key, action=action, query=query,
                                           _rule_hops=_rule_hops, new_window=new_window)
            except Exception:
                pass
    except Exception:
        pass

    # 1) Try app_registry.json first (fresh scan), then capabilities.json.
    entry = None
    try:
        from cygnus.apps.capabilities import load_registry, resolve_normalized
        reg = load_registry()
        if reg:
            n = requested.lower().strip()
            apps = reg.get("apps", {})
            # 1a) exact, then normalized ('snipping tool' == 'snippingtool').
            # Lookup-only: no guessing before these two run.
            for key in (n, requested.lower()):
                if key in apps:
                    entry = apps[key]
                    break
            if not entry:
                nk = resolve_normalized(apps, n)
                if nk:
                    entry = apps[nk]
            # 1b) WORD-BOUNDARY substring. Raw 'in' matching once opened
            # fold.exe for "documents folder" ('fold' sits inside the word
            # 'folder'); \b...\b requires the key to be a whole word.
            # 2026-08-25: most-specific wins — longest matching whole-word key
            # ('git bash' beats 'git').
            if not entry:
                import re as _re
                # Reverse containment (spoken ⊆ key) allowed only for
                # multi-word requests: single generic words ('updater')
                # must not hijack a random app whose NAME contains them.
                cands = []
                for key in apps:
                    if _re.search(rf"\b{_re.escape(key)}\b", n):
                        cands.append(key)
                    elif (len(n.split()) >= 2
                          and _re.search(rf"\b{_re.escape(n)}\b", str(key))):
                        cands.append(key)
                if cands:
                    best = max(cands, key=lambda k: (len(k), ))
                    entry = apps[best]
            # Friendly product names whose exe keys share no word with them
            # ("microsoft word" vs key "winword"). Map to the exe stem and
            # retry exact/substring before falling to token scoring.
            # 2026-08-25: substring `stem in key` was raw keyword guessing —
            # 'code' matched INSIDE 'frcode' and launched Git's locate helper
            # for "open vscode". Now: exact stem only; if absent, look for the
            # stem as a PATH component (...\bin\code.cmd) among launchable
            # entries, preferring the shortest key.
            if not entry:
                from cygnus.apps.capabilities import _is_launchable as _stem_launchable
                _PRODUCT_EXE = _product_exe_map()
                for phrase, exe_stem in _PRODUCT_EXE.items():
                    if phrase in n:
                        if exe_stem in apps:
                            entry = apps[exe_stem]
                            break
                        # stem not a key: accept the stem as a PATH component
                        # (...\Microsoft VS Code\bin\code.cmd) or exact file
                        # (Code.exe is 'code' case-insensitively) — never a
                        # bare substring of an unrelated key name.
                        by_path = [
                            (key, apps[key]["bin"]) for key in apps
                            if apps[key].get("bin") and re.search(
                                rf"[\\/]{_re.escape(exe_stem)}(\.[a-z0-9]+)?[\\/]"
                                rf"|\\b{_re.escape(exe_stem)}\.(?:exe|cmd|lnk)$",
                                str(apps[key]["bin"]), re.I)
                            # exclude helper/tunnel/CLI siblings of the app
                            # dir from the path-component match; prefer the
                            # main GUI exe by scoring: shorter key = better,
                            # and 'tunnel'/'helper' bins demoted.
                            and not re.search(r"tunnel|helper|crash",
                                              str(apps[key]["bin"]), re.I)
                            and _stem_launchable(apps[key].get("bin"))]
                        if by_path:
                            best_key = min(by_path, key=lambda kv: len(kv[0]))
                            entry = {"bin": best_key[1], "kind": "gui",
                                     "name": best_key[0]}
                            break
                        # last resort: same stem rule against key NAMES as
                        # whole words ('vs code' -> 'visual studio code'),
                        # excluding component bins (tunnel/helper/crash) and
                        # preferring the key that is NOT itself a component.
                        by_word = [key for key in apps
                                   if re.search(
                                       rf"\b{_re.escape(exe_stem)}\b",
                                       str(key), re.I)
                                   and not re.search(
                                       r"tunnel|helper|crash|setup|update",
                                       str(apps[key].get("bin", "")), re.I)
                                   and _stem_launchable(apps[key].get("bin"))]
                        if by_word:
                            best_key = min(by_word, key=len)
                            entry = apps[best_key]
                            break
            # Token-overlap scoring: fraction of the registry key's words found
            # in the request ("microsoft office home 2024" vs "office 2024").
            if not entry:
                n_words = set(n.split()) - {"microsoft", "ms", "the", "app", "application"}
                best, best_score = None, 0.0
                for key in apps:
                    k_words = set(key.split())
                    if not k_words:
                        continue
                    inter = n_words & k_words
                    if not inter:
                        continue
                    score = len(inter) / min(len(k_words), max(1, len(n_words)))
                    if score > best_score:
                        best_score = score
                        best = key
                if best and best_score >= 0.5:
                    entry = apps[best]
    except Exception:
        pass
    if not entry:
        try:
            from cygnus.apps.capabilities import resolve as _resolve
        except Exception:
            try:
                from cygnus.apps.capabilities import resolve as _resolve
            except Exception:
                _resolve = None
        # Resolve-before-act. resolve() returns the FIRST substring match and
        # throws the rest away, so an underspecified name ("open ch" -> 46
        # manifest keys) launched an arbitrary app with full confidence. Ask
        # when the name is genuinely ambiguous; stay instant when it is not.
        # Only this last-chance fuzzy step is gated — the registry paths above
        # already match on word boundaries and longest-key-wins.
        if _resolve:
            try:
                from cygnus.apps.capabilities import resolve_candidates as _rc
                _tier, _cands = _rc(requested)
                _launchable_cands = []
                try:
                    from cygnus.apps.capabilities import _is_launchable as _lc
                    _launchable_cands = [(k, v) for k, v in _cands
                                         if _lc(v.get("bin"))]
                except Exception:
                    _launchable_cands = list(_cands)
                if _tier in ("prefix", "substring") and len(_launchable_cands) > 1:
                    _names = ", ".join(k for k, _ in _launchable_cands[:5])
                    _audit_log("open_application", requested, "AMBIGUOUS",
                               result=f"{len(_launchable_cands)} candidates")
                    try:                     # "the second one" / "codium" answers it
                        from cygnus.brain import dialogue_state
                        dialogue_state.ask("pick", "Which one should I open?",
                                           {"verb": "open",
                                            "options": [k for k, _ in _launchable_cands[:5]]},
                                           owner="tools")
                    except Exception:
                        pass
                    return (f"[NEEDS_PICK] \"{requested}\" matches "
                            f"{len(_launchable_cands)} applications: {_names}. "
                            f"Which one should I open?")
                entry = _launchable_cands[0][1] if _launchable_cands else _resolve(requested)
            except Exception:
                entry = _resolve(requested)
        else:
            entry = None
    # Phase 14b: never launch a non-launchable bin. If the registry handed us a
    # directory / document / MSI icon string, drop the entry and keep searching
    # via aliases + raw name instead of startfile-ing junk.
    # 2026-08-25: this ALSO drops component exes (installer/updater/helper) —
    # capabilities.json fallback can still surface them ('updater' ->
    # LibreOffice updater.exe), so re-check here, never trust the source.
    from cygnus.apps.capabilities import _is_launchable as _launchable
    if entry and not _launchable(entry.get("bin")):
        entry = None
    binp = entry.get("bin") if entry else None
    name = entry.get("name", requested) if entry else requested

    # Phase 3.5 runtime enforcement: consult the apps' compiled_rules.
    # Rules compile + store (see rules_compiler.py) but nothing enforced them
    # until now. A hard rule blocks the launch outright; a soft rule surfaces
    # as a reminder the caller can render to the user.
    try:
        from cygnus.rules.engine import evaluate_app_rules, format_rule_notices
        _verdict = evaluate_app_rules(entry, action)
        if not _verdict["allowed"]:
            _blocks = "\n".join(_verdict["blocks"])
            return (f"[Blocked] {name} was not opened.\n{_blocks}")
        _notices = format_rule_notices(_verdict["notices"])
    except Exception:
        # Enforcement must never break a launch; fail open.
        _notices = ""

    # Phase 3 gate: rules_engine.check() adds deny + redirect on top of the
    # hard/soft verdict above. A deny stops the launch; a redirect opens the
    # rule's target app instead (one hop only — a rule chain must not loop).
    try:
        from cygnus.rules import engine as _rules
        _gate = _rules.check(requested,
                             {"action": action, "entry": entry})
    except Exception:
        _gate = {"action": "allow"}
    if _gate.get("action") == "deny":
        return f"[Error] Launch blocked by rule: {_gate.get('reason', '')}"
    if _gate.get("action") == "redirect" and _gate.get("target"):
        if _rule_hops >= 1:
            return (f"[Error] Launch blocked by rule: redirect loop while "
                    f"opening {name}.")
        return open_application(_gate["target"], action=action, query=query,
                               _rule_hops=_rule_hops + 1, new_window=new_window)
    if _gate.get("warning"):
        _notices = f"{_notices}\n{_gate['warning']}".strip()

    # Already open: bring its window forward. Launching again opens a second
    # window, and a second Office program opens an empty document.
    if not action and not new_window:
        _front = _focus_running([requested, name], (entry or {}).get("exe") or binp)
        if _front:
            return _front

    # 2) Spotify (or other music) search: reliable URI opens the Search pane.
    if action == "search" and "spotify" in name.lower():
        q = (query or "").strip()
        uri = "spotify:search:" + q.replace(" ", "%20")
        try:
            _os.startfile(uri)
            _smsg = (f"Opened Spotify search for '{q}'. The Search pane is showing "
                     f"results for '{q}' — pick the track and press play.")
            if _notices:
                _smsg += f"\n\n{_notices}"
            return _smsg
        except Exception as e:
            return f"[Error] Could not open Spotify search URI: {e}"

    # 3) Launch the resolved binary / alias / raw name.
    cand = binp or target

    # If no binary was resolved, check whether this is a website registered in
    # web_registry.json. Websites carry a URL but no executable, so startfile()
    # on the app name always fails with WinError 2. Route them to open_site().
    if not binp:
        if entry and entry.get("url"):
            try:
                return _open_url_in_browser(entry["url"], name)
            except Exception as e:
                return f"[Error] Could not open {name}: {e}"
        try:
            from cygnus.apps import web_registry as _wr
            _sites = (_wr.load_registry() or {}).get("sites", {})
            if requested.lower() in _sites:
                return open_site(requested)
            _sk = _wr.resolve_site(requested)
            if _sk:
                return open_site(requested)
        except Exception:
            pass

    try:
        if system == "Windows":
            _os.startfile(cand)
        elif system == "Darwin":
            subprocess.run(["open", "-a", cand])
        else:
            subprocess.run(["xdg-open", cand])
        _note_opened(name, entry)
        _msg = f"Opened {name}"
        if _notices:
            _msg += f"\n\n{_notices}"
        return _msg
    except Exception as e:
        # Last chance: strip/add .exe before giving up.
        alt = cand[:-4] if cand.lower().endswith(".exe") else cand + ".exe"
        try:
            if system == "Windows":
                _os.startfile(alt)
                _note_opened(name, entry)
                return f"Opened {name}"
        except Exception:
            pass
        # 6) Broken shortcut? Try to find a replacement by re-scanning.
        try:
            from cygnus.apps.capabilities import find_replacement, REGISTRY_PATH
            repair = find_replacement(requested)
            if repair:
                # Update the registry with the fix
                from cygnus.apps.capabilities import load_registry
                reg = load_registry() or {"apps": {}}
                reg["apps"][repair["name"]] = {
                    "bin": repair["bin"], "kind": "gui",
                    "category": _categorize(repair["name"]),
                    "confidence": "repaired"}
                with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                    json.dump(reg, f, indent=2)
                if system == "Windows":
                    _os.startfile(repair["bin"])
                return f"Repaired and opened {repair['name']}"
        except Exception:
            pass
        if not entry:
            return (f"[Error] Could not open {requested}: {e}. "
                    f"Say 'scan installed software' to refresh the manifest.")
        return f"[Error] Could not open {name}: {e}"


def rescan_applications() -> str:
    """Rescan all installed software and update the app registry.
    Reports broken shortcuts found during the scan."""
    from cygnus.apps.capabilities import write_registry, load_registry
    result = write_registry()
    reg = load_registry()
    if reg:
        broken = []
        for name, entry in reg.get("apps", {}).items():
            binp = entry.get("bin")
            if binp and not os.path.exists(binp):
                broken.append(name)
        if broken:
            result += f"\n{len(broken)} broken shortcuts: {', '.join(broken[:15])}"
            if len(broken) > 15:
                result += f" (and {len(broken)-15} more)"
    return result


def _find_app_by_name(name: str) -> str | None:
    """Resolve a spoken app name to a registry key. Mirrors the lookup parts
    of open_application: exact -> normalized -> substring (longest wins) ->
    alias -> product_exe -> token scoring."""
    from cygnus.apps.capabilities import load_registry, resolve_normalized
    from cygnus.apps.curate import is_registered
    if not name:
        return None
    reg = load_registry()
    if not reg:
        return None
    apps = reg.get("apps", {})
    n = name.lower().strip()
    # exact
    if n in apps:
        return n
    # normalized
    nk = resolve_normalized(apps, n)
    if nk:
        return nk
    # alias
    target = APP_ALIASES.get(n, n)
    if target.lower() in apps:
        return target.lower()
    # substring (longest key wins)
    cands = []
    for key in apps:
        if re.search(rf"\b{re.escape(key)}\b", n):
            cands.append(key)
    if cands:
        return max(cands, key=lambda k: len(k))
    # product_exe
    _PRODUCT_EXE = _product_exe_map()
    for phrase, exe_stem in _PRODUCT_EXE.items():
        if phrase in n and exe_stem in apps:
            return exe_stem
    # token scoring
    n_words = set(n.split()) - {"microsoft", "ms", "the", "app", "application"}
    best, best_score = None, 0.0
    for key in apps:
        k_words = set(key.split())
        if not k_words:
            continue
        inter = n_words & k_words
        if not inter:
            continue
        score = len(inter) / min(len(k_words), max(1, len(n_words)))
        if score > best_score and score >= 0.5:
            best_score = score
            best = key
    return best


def install_app(app: str, enable: bool = True) -> str:
    """Install an app into JARVIS: opt it into the registered+enabled set so
    voice commands can resolve to it. 'Installing' = curating the local registry;
    it does NOT touch the actual Windows program."""
    from cygnus.apps.capabilities import REGISTRY_PATH
    from cygnus.apps.curate import is_registered, mark_registered
    key = _find_app_by_name(app)
    if not key:
        return (f'[Error] I scanned the registry but did not find "{app}". '
                f'Say "rescan" to refresh the list, or try a more specific name.')
    ok = mark_registered(key, registered=True, enabled=enable)
    if not ok:
        return f'[Error] Found "{key}" in the registry but could not register it.'
    state = "enabled" if enable else "disabled"
    return f'Installed "{key}" into Cygnus — registered and {state}.'


def uninstall_app(app: str) -> str:
    """Remove an app from Cygnus's voice registry (opt it out). The program
    stays installed on Windows; JARVIS just stops routing voice commands to it."""
    from cygnus.apps.curate import is_registered, mark_registered
    key = _find_app_by_name(app)
    if not key:
        return f'I could not find "{app}" in the registry. It may not be installed on this machine.'
    if not is_registered(key):
        return f'"{key}" is already not installed in Cygnus — it is not in your active app set.'
    ok = mark_registered(key, registered=False)
    if not ok:
        return f'[Error] Found "{key}" but could not remove it.'
    return f'Removed "{key}" from Cygnus. Say "rescan my apps" to bring it back as a candidate.'


def list_installed_apps() -> str:
    """List all apps currently installed in JARVIS (registered + enabled)."""
    from cygnus.apps.curate import registered_apps
    apps = registered_apps()
    if not apps:
        return "Your Cygnus app catalog is empty, sir. Say \"install <app name>\" to add one."
    lines = [f"You have {len(apps)} app(s) installed in Cygnus:"]
    for a in sorted(apps):
        lines.append(f"  * {a}")
    return "\n".join(lines)


def _running_row_for(said):
    """The Running-panel row whose name the user said, or None."""
    try:
        from cygnus.apps import running as running_apps
        return running_apps.app_window([said], rows=running_apps.inventory())
    except Exception:
        return None


def _close_row_windows(row):
    """Ask each window of a Running row to close (WM_CLOSE, so an app may
    still ask to save). True when at least one window was asked."""
    if not row or not row.get("windows"):
        return False
    try:
        import ctypes
        asked = False
        for w in row["windows"]:
            if w.get("hwnd"):
                ctypes.windll.user32.PostMessageW(int(w["hwnd"]), 0x0010, 0, 0)
                asked = True
        return asked
    except Exception:
        return False


def _row_exe(row):
    """The program file behind a Running row, without .exe: the one named
    like the row ("chrome.exe" for Google Chrome, "windowsterminal.exe" for
    Windows Terminal), else the first."""
    exes = [e for e in row.get("exes") or [] if e.endswith(".exe")]
    if not exes:
        return ""
    words = re.findall(r"[a-z0-9]+", (row.get("name") or "").lower())
    joined = "".join(words)
    for e in exes:
        stem = e[:-4]
        if stem in words or stem.replace("-", "").replace("_", "") == joined:
            return stem
    for e in exes:
        if set(re.findall(r"[a-z0-9]+", e[:-4])) & set(words):
            return e[:-4]
    return exes[0][:-4]


def close_application(app: str, force: bool = False) -> str:
    """Close a running application by friendly name, via registry lookup.

    Resolves the app exactly like open_application, derives its exe name,
    then asks it to quit gracefully (WM_CLOSE) before force-killing.
    """
    requested = _clean_app_name(app)
    # Strip the close verb itself — callers may pass the whole utterance
    # ('close it', 'close the spotify') rather than a bare app name.
    requested = re.sub(
        r"^(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?"
        r"(?:close|quit|exit|kill|shut down)\s+(?:the\s+|my\s+|a\s+)*",
        "", requested, flags=re.IGNORECASE).strip()
    if requested.lower() in {"it", "this", "that", "app", "application",
                             "the", "a", "my", "", "that app", "this app", "the app"}:
        # "Open Calculator" ... "close it": the app just opened or shown.
        try:
            from cygnus.brain import dialogue_state
            last = dialogue_state.last_result("app")
        except Exception:
            last = None
        if last and (last.get("app") or last.get("title")):
            requested = _clean_app_name(str(last.get("app") or last.get("title")))
        else:
            return "[Error] Which app should I close? Name it, e.g. 'close Spotify'."
    if requested.lower() in {"yourself", "you", "jarvis", "yourself jarvis", "cygnus", "yourself cygnus"}:
        return ("I can't close myself, sir. Close the Cygnus window, or say "
                "\"go to sleep\" to end the session.")

    # Resolve through the same chain as open_application.
    entry = None
    try:
        from cygnus.apps.capabilities import load_registry, resolve as _resolve
        reg = load_registry()
        if reg:
            n = requested.lower().strip()
            apps = reg.get("apps", {})
            if n in apps:
                entry = apps[n]
            else:
                for key in apps:
                    if n in key or key in n:
                        entry = apps[key]
                        break
            if not entry:
                _PRODUCT_EXE = _product_exe_map()
                for phrase, exe_stem in _PRODUCT_EXE.items():
                    if phrase in n and exe_stem in apps:
                        entry = apps[exe_stem]
                        break
        if not entry:
            entry = _resolve(requested)
    except Exception:
        entry = None

    binp = (entry or {}).get("bin") or ""
    # A Store app's bin is a shell:AppsFolder address; its program file is
    # the registry's `exe` (Telegram.exe, CalculatorApp.exe).
    exe_file = (entry or {}).get("exe") or ""
    if not exe_file and binp and not binp.lower().startswith("shell:"):
        exe_file = os.path.basename(binp.strip('"'))
    exe_name = os.path.splitext(exe_file)[0] if exe_file else ""
    name = (entry or {}).get("label") or (entry or {}).get("name", requested)

    # Phase 3.5 runtime enforcement (close path): consult the app's
    # compiled_rules for a hard "must not close" guard. Like open_application,
    # a HARD rule blocks outright; soft rules are reminders (not applicable to
    # closing, which is a single irreversible action, so only hard guards act).
    try:
        from cygnus.rules.engine import evaluate_app_rules, format_rule_notices
        _close_verdict = evaluate_app_rules(entry, "close")
        if not _close_verdict["allowed"]:
            return " ".join(_close_verdict["blocks"])
        _close_notices = format_rule_notices(_close_verdict["notices"])
    except Exception:
        _close_notices = ""

    if not exe_name:
        # Not in the registry: the Running panel knows what is open by its
        # window and program file ("close windows terminal" -> WindowsTerminal.exe).
        row = _running_row_for(requested)
        if row:
            exe_name, name = _row_exe(row), row["name"]
    if not exe_name:
        # No registry entry: try the raw name as an image name.
        exe_name = requested

    def _running(image: str) -> bool:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}.exe"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return image.lower() in (r.stdout or "").lower()

    if not _running(exe_name):
        return f"{name} is not running."

    # 0) The window the Running panel shows gets WM_CLOSE first. A Store
    # app's window belongs to ApplicationFrameHost, so closing "its" main
    # window (step 1) does nothing to Calculator or Settings.
    if _close_row_windows(_running_row_for(requested) or _running_row_for(name)):
        time.sleep(2)
        if not _running(exe_name):
            return f"Closed {name}."

    # 1) Graceful: WM_CLOSE to all windows of the process.
    ps = (f"Get-Process -Name '{exe_name}' -ErrorAction SilentlyContinue | "
          "ForEach-Object { $_.CloseMainWindow() } | Out-Null")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=15)
    time.sleep(2)
    if not _running(exe_name):
        return f"Closed {name}."

    # 2) taskkill without /F (sends WM_QUIT): still a polite close.
    subprocess.run(["taskkill", "/IM", f"{exe_name}.exe"],
                   capture_output=True, timeout=15)
    time.sleep(1)
    if not _running(exe_name):
        return f"Closed {name}."

    # 3) Ending the process loses unsaved work (Notepad asking "Save?" was
    # force-killed after 5 s on 2026-09-11), so only when the user says so.
    if not force:
        return (f"{name} is still open; it may be asking to save. Say "
                f"“force close {name}” to end it anyway.")
    subprocess.run(["taskkill", "/IM", f"{exe_name}.exe", "/F"],
                   capture_output=True, timeout=15)
    time.sleep(1)
    if not _running(exe_name):
        return f"Force-closed {name}."
    return f"[Error] Could not close {name} ({exe_name}.exe is still running)."


def _resolve_spotify_track(query: str) -> str:
    """Resolve a free-text song/artist query to a spotify:track:<id> URI using the
    public open.spotify.com search page rendered headlessly (no auth, no Premium).
    Returns the URI string or '' if resolution failed."""
    import re as _re
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return ""
    q = (query or "").strip()
    if not q:
        return ""
    # Already a URI? pass through.
    if q.startswith("spotify:"):
        return q
    uri = ""
    try:
        import tempfile as _tf
        _prof = _tf.mkdtemp(prefix="jv_spotify_res_")
        with sync_playwright() as p:
            # launch_persistent_context (NOT launch + --user-data-dir, which Playwright rejects)
            ctx = p.chromium.launch_persistent_context(_prof, headless=True)
            pg = ctx.new_page()
            # Spotify is JS-rendered and rate-limits headless scrapers, so retry a few
            # times, each waiting for a real track link + scrolling to force lazy-load.
            enc = _re.sub(r"\s+", "%20", q)
            seen = []
            for attempt in range(3):
                try:
                    pg.goto(f"https://open.spotify.com/search/{enc}",
                            wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
                # wait for a track anchor, up to ~8s
                try:
                    pg.wait_for_selector("a[href*='/track/']", timeout=8000)
                except Exception:
                    pass
                # scroll the results grid to trigger lazy rendering
                for _ in range(3):
                    pg.mouse.wheel(0, 1200)
                    pg.wait_for_timeout(800)
                uris = _re.findall(r"spotify:track:[A-Za-z0-9]+", pg.content())
                if not uris:
                    # open.spotify.com serves results as JS-rendered <a href="/track/<id>">
                    # links; the spotify:track: form is no longer in the HTML, so read
                    # the hrefs directly and convert them to spotify:track: URIs.
                    try:
                        hrefs = pg.eval_on_selector_all(
                            'a[href*="/track/"]',
                            'els => els.map(e => e.getAttribute("href") || "").filter(Boolean)')
                        for h in hrefs:
                            m = _re.search(r"/track/([A-Za-z0-9]+)", h or "")
                            if m:
                                uris.append("spotify:track:" + m.group(1))
                    except Exception:
                        pass
                for u in uris:
                    if u not in seen:
                        seen.append(u)
                if seen:
                    break
                pg.wait_for_timeout(2500)  # back off before retry
            uri = seen[0] if seen else ""
            ctx.close()
    except Exception:
        uri = ""
    return uri


def _cap_spotify_session_volume(cap: float, wait_s: float = 8.0) -> bool:
    """Lower Spotify's own Windows audio-session volume to at most `cap` (0..1).

    Per-process mixer only (pycaw, same mechanism as voice_ducking) — never the
    system master volume. Spotify's session only exists once audio starts, so
    poll briefly after playback. Returns True if a Spotify session was capped."""
    import time as _t
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
    except Exception:
        return False
    deadline = _t.monotonic() + wait_s
    while True:
        capped = False
        try:
            sessions = AudioUtilities.GetAllSessions()
        except Exception:
            sessions = []
        for s in sessions:
            try:
                if not s.Process or s.Process.name().lower() != "spotify.exe":
                    continue
                vol = getattr(s, "SimpleAudioVolume", None)
                if vol is None:
                    vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                if vol.GetMasterVolume() > cap:
                    vol.SetMasterVolume(cap, None)
                capped = True
            except Exception:
                continue
        if capped or _t.monotonic() >= deadline:
            return capped
        _t.sleep(0.5)


def play_spotify(query: str) -> str:
    """Play a song/artist/album on the user's DESKTOP Spotify app (the spicetify-
    patched install), hands-free — no Premium, no credentials, no GUI clicks.

    Flow: if `query` is already a spotify: URI, open it directly; otherwise resolve
    the name to a track URI via the public search page, then `os.startfile` the
    track URI, which makes the desktop app start playback. Returns a short status
    string for speech. On any failure it says plainly what went wrong."""
    import os as _os
    # Honour the Spotify volume-cap rule here too — music_agent enforces it on
    # web playback; this is the desktop-app path.
    try:
        try:
            from cygnus.agents import music_rules as _music_rules
        except ImportError:
            import sys as _sys
            from cygnus.agents import music_rules as _music_rules
        cap = _music_rules.resolve_music_rules(app_key="spotify").get("cap_volume")
    except Exception:
        cap = None
    q = (query or "").strip()
    if not q:
        return "Say what you'd like to play, sir."
    # Ensure Spotify is running (the desktop app must be open to receive the URI).
    try:
        from cygnus.apps.capabilities import resolve as _resolve
    except Exception:
        try:
            from cygnus.apps.capabilities import resolve as _resolve
        except Exception:
            _resolve = None
    spot = _resolve("spotify") if _resolve else None
    if spot and spot.get("bin"):
        try:
            _os.startfile(spot["bin"])
        except Exception:
            pass
        import time as _t
        _t.sleep(3)
    uri = None
    try:
        # Track resolution scrapes open.spotify.com in a headless browser —
        # cold, that wedges past two minutes (live probe: total silence).
        # Bound it: on expiry answer honestly instead of hanging the turn.
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
            uri = _ex.submit(_resolve_spotify_track, q).result(timeout=45)
    except Exception:
        uri = None
    if not uri:
        return (f"I couldn't find a Spotify track for '{q}', sir. Try a more "
                f"specific title or artist.")
    try:
        _os.startfile(uri)
        msg = f"Playing {q} on Spotify, sir."
        if cap is not None and _cap_spotify_session_volume(cap):
            msg += f" (volume capped at {int(round(cap * 100))}%)"
        return msg
    except Exception as e:
        return f"[Error] Could not start playback: {e}"


# The player Cygnus last started. "pause the music" went to the browser
# player only: after play_spotify it answered "Nothing is playing - the music
# window is not open" while Spotify played on (2026-09-22).
_LAST_PLAYER = {"name": None}


def _played(player: str, result):
    if not str(result or "").startswith("[Error]"):
        _LAST_PLAYER["name"] = player
    return result


def _stop_last_player() -> str:
    from cygnus.agents import music as music_agent
    music_agent.set_progress_cb(getattr(_tools_tls, "cb", None))
    out = music_agent.stop_music()
    if str(out).startswith("Nothing is playing") and _LAST_PLAYER["name"] == "spotify":
        # Spotify pauses by the play/pause media key, which toggles: pressed
        # only once for the play Cygnus started, never on a second "pause".
        _LAST_PLAYER["name"] = None
        return stop_spotify()
    return out


def stop_spotify() -> str:
    """Pause the desktop Spotify app by sending the system MediaPlayPause key to the
    foreground window (no auth, no GUI clicks). Returns a short status string."""
    try:
        import ctypes
        # VK_MEDIA_PLAY_PAUSE = 0xB3
        ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0)
        ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
        return "Paused Spotify, sir."
    except Exception as e:
        return f"[Error] Could not pause Spotify: {e}"


def get_weather(city: str = "Manila") -> str:
    """Get current weather for a city via wttr.in.

    Fetched in-process rather than by shelling out to curl: wttr.in replies in
    UTF-8, and subprocess text mode decodes with the Windows cp1252 locale,
    which blows up on the weather glyphs and loses the whole response.
    """
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%C+%t+feels+like+%f+humidity+%h+wind+%w"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        return f"[Error] Weather lookup failed for {city}: {type(e).__name__}: {e}"
    if not text or "Unknown location" in text:
        return f"[Error] Weather service returned no data for {city}"
    # wttr encodes wind direction as an arrow glyph; spell it out for text-to-speech.
    for arrow, word in (("↑", "from the south"), ("↓", "from the north"),
                        ("←", "from the east"), ("→", "from the west"),
                        ("↖", "from the southeast"), ("↗", "from the southwest"),
                        ("↘", "from the northwest"), ("↙", "from the northeast")):
        text = text.replace(arrow, word + " ")
    return text


# The legacy TOOLS list (OpenAI-style declarations read only by the deleted
# brain.py) was removed in restructure phase 5; brain/core.py declares the
# tools the models see.


def _truthy(v) -> bool:
    """Strict opt-in. Models emit "true"/"True"/1 as often as real booleans, but
    anything unrecognised must fall back to NOT sending."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return False


def _ask_chatgpt(**kw) -> str:
    from cygnus.browser import agent as browser_agent
    submit = _truthy(kw.get("submit", False))
    # Audit line: every send decision the model made is recoverable from the log.
    print(f"[TOOL] ask_chatgpt submit={submit} (raw={kw.get('submit')!r}) "
          f"prompt={kw['prompt'][:80]!r}", flush=True)
    return browser_agent.ask_chatgpt(prompt=kw["prompt"], submit=submit)


def _ask_ai(**kw) -> str:
    """Write a prompt into a web AI (chatgpt/gemini/claude/copilot) or the
    Copilot desktop app. Sends ONLY on explicit consent."""
    from cygnus.browser import agent as browser_agent
    submit = _truthy(kw.get("submit", False))
    site = (kw.get("site") or "chatgpt").strip()
    prompt = kw.get("prompt") or ""
    print(f"[TOOL] ask_ai site={site} submit={submit} "
          f"prompt={prompt[:80]!r}", flush=True)
    if site.lower() in {"copilot desktop", "copilotapp"}:
        return _ask_copilot_desktop(prompt, submit)
    return browser_agent.ask_web_ai(prompt=prompt, site=site, submit=submit)


def _ask_copilot_desktop(prompt: str, submit: bool) -> str:
    """Drive the installed Microsoft Copilot app via computer control:
    open it, type the prompt; send only on consent."""
    import subprocess as _sp, time as _time
    exe = r"C:\Program Files (x86)\Microsoft\EdgeCore\151.0.4129.101\copilotapp.exe"
    if not os.path.exists(exe):
        return "[Error] The Copilot desktop app is not at its expected path."
    try:
        _sp.Popen([exe])
        _time.sleep(4)
        import pyautogui
        pyautogui.typewrite(prompt, interval=0.02)
        if not submit:
            return (f"Typed into the Copilot app and left it unsent: {prompt}. "
                    "Say the word and I'll send it.")
        pyautogui.press("enter")
        _time.sleep(8)
        return f"Sent to the Copilot desktop app: {prompt}. Its reply is on screen."
    except Exception as e:
        return f"[Error] Driving the Copilot app failed: {e}"


# Tool dispatcher
TOOL_MAP = {
    "read_file": lambda **kw: read_file(kw["path"]),
    "write_file": lambda **kw: write_file(kw["path"], kw["content"],
                                          _truthy(kw.get("overwrite", False))),
    "write_to_notepad": lambda **kw: write_to_notepad(kw["content"], kw.get("filename", "")),
    "list_directory": lambda **kw: list_directory(kw.get("path", ".")),
    "search_chatgpt_history": lambda **kw: __import__("cygnus.browser.agent", fromlist=["_"]).search_chatgpt_history(
        kw["query"], int(kw.get("limit", 10) or 10)),
    "open_chatgpt_conversation": lambda **kw: __import__("cygnus.browser.agent", fromlist=["_"]).open_chatgpt_conversation(
        kw["title_contains"]),
    "search_web": lambda **kw: search_web(kw["query"]),
    "open_site": lambda **kw: open_site(kw["name"], kw.get("url"), browser=kw.get("browser")),
    "rescan_sites": lambda **kw: rescan_sites(),
    "list_sites": lambda **kw: __import__("cygnus.apps.web_registry", fromlist=["_"]).list_sites(),
    "add_site": lambda **kw: __import__("cygnus.apps.web_registry", fromlist=["_"]).add_manual_site(
        kw["name"], kw["url"], kw.get("aliases")),
    "search_sessions": lambda **kw: search_sessions(
        kw["query"], int(kw.get("limit", 10) or 10)),
    "search_vault": lambda **kw: search_vault(
        kw["query"], int(kw.get("limit", 10) or 10)),
    "search_vault_semantic": lambda **kw: search_vault_semantic(
        kw["query"], int(kw.get("limit", 10) or 10)),
    "read_vault_note": lambda **kw: read_vault_note(kw["name"]),
    "recall_facts": lambda **kw: __import__("cygnus.memory.semantic", fromlist=["_"]).recall(
        kw["query"], int(kw.get("limit", 5) or 5)),
    "desktop_control": _desktop_control_tool,
    "run_opencli": _run_opencli_tool,
    "open_application": lambda **kw: open_application(
        kw["app"], kw.get("action"), kw.get("query"), new_window=bool(kw.get("new_window"))),
    "ask_chatgpt": _ask_chatgpt,
    "ask_ai": _ask_ai,
    "manus_status": lambda **kw: __import__("cygnus.browser.manus", fromlist=["_"]).manus_status(),
    "browser_status": lambda **kw: __import__("cygnus.browser.agent", fromlist=["_"]).browser_status(),
    "play_music": lambda **kw: _played("browser", (
        __import__("cygnus.agents.music", fromlist=["_"]).set_progress_cb(getattr(_tools_tls, "cb", None)),
        __import__("cygnus.agents.music", fromlist=["_"]).play_music(kw["query"])
    )[-1]),
    "stop_music": lambda **kw: _stop_last_player(),
    "play_spotify": lambda **kw: _played("spotify", play_spotify(kw["query"])),
    "stop_spotify": lambda **kw: stop_spotify(),
    "set_volume": lambda **kw: __import__("cygnus.apps.volume", fromlist=["_"]).control(
        kw.get("action", ""), kw.get("percent")),
    "get_credentials": _get_credentials_tool,
    "launch_project": lambda **kw: __import__("cygnus.agents.project", fromlist=["_"]).launch_project(kw["name"]),
    "compose_report": lambda **kw: __import__("cygnus.agents.report", fromlist=["_"]).compose_report(
        kw["topic"], kw.get("output_path"), _truthy(kw.get("verbatim", False))),
    "rescan_applications": lambda **kw: rescan_applications(),
    "install_app": lambda **kw: install_app(kw["app"], _truthy(kw.get("enable", True))),
    "uninstall_app": lambda **kw: uninstall_app(kw["app"]),
    "list_installed_apps": lambda **kw: list_installed_apps(),
    "close_application": lambda **kw: close_application(kw["app"], force=bool(kw.get("force"))),
    "run_autonomous": lambda **kw: run_autonomous(
        kw["goal"], int(kw.get("timeout", 1800) or 1800), _truthy(kw.get("confirm", False))),
    "job_control": lambda **kw: job_control(
        kw["action"], kw.get("jid")),
    "delegate_task": lambda **kw: delegate_task(kw["task"], kw.get("agent", "claude")),
    "delegate_to_hermes": lambda **kw: delegate_to_hermes(
        kw["task"], int(kw.get("timeout", 300) or 300),
        int(kw.get("max_turns", 6) or 6), _truthy(kw.get("confirm", False)), **_job_run(kw)),
    "delegate_to_hermes_grounded": lambda **kw: delegate_to_hermes_grounded(
        kw["task"], int(kw.get("timeout", 300) or 300),
        int(kw.get("max_turns", 6) or 6), _truthy(kw.get("confirm", False)), **_job_run(kw)),
    "web_browse": lambda **kw: web_browse(kw["url"], kw.get("mode", "open"), kw.get("query", "")),
    "delegate": lambda **kw: delegate(
        kw["task"], kw.get("backend"),
        _truthy(kw.get("confirm", False)),
        _truthy(kw.get("grounded", True)),
        timeout=int(kw.get("timeout", 300) or 300),
        max_turns=int(kw.get("max_turns", 15) or 15), **_job_run(kw)),
    "get_memory_context": lambda **kw: get_memory_context(int(kw.get("tokens", 4000))),
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name with arguments. Quarantines external text, audits structured."""
    if name not in TOOL_MAP:
        _audit_log(f"execute_tool:{name}", json.dumps(arguments, default=str)[:500], "unknown_tool", result=f"[Error] Unknown tool: {name}")
        return f"[Error] Unknown tool: {name}"
    print(f"[TOOL] {name} args={json.dumps(arguments, default=str)[:200]}", flush=True)
    # The one safety gate (safety.py): a confirm flag the model set counts only
    # when the tool asked in an earlier turn, and an action that needs a
    # confirm is held until the user says yes. _safety_ok marks the held call
    # being run after that yes.
    from cygnus import safety
    arguments = dict(arguments or {})
    approved = bool(arguments.pop("_safety_ok", False))
    if name in safety.SELF_LATCHED and _truthy(arguments.get("confirm", False))             and not safety.confirm_flag_allowed(name):
        print(f"[SAFETY] {name}: confirm set in the turn that asked; asking the user", flush=True)
        arguments["confirm"] = False
    if not approved:
        decision = safety.assess(name, arguments)
        if decision.kind != "act":
            held_args = dict(arguments)
            return safety.gate(f"{name} {json.dumps(held_args, default=str)[:200]}", decision,
                               lambda: execute_tool(name, {**held_args, "_safety_ok": True}))
    start = time.monotonic()
    confirmed = _truthy(arguments.get("confirm", False)) or approved
    # Phase 1b: quarantine instruction-like content in arguments that came from external sources
    # (e.g., pasted browser text). This is data, not directions.
    try:
        from cygnus.browser import guard
        for k, v in list(arguments.items()):
            if isinstance(v, str) and len(v) > 20 and guard.is_suspicious(v):
                arguments[k] = guard.sanitize_web_text(v, label=f"tool:{name}:{k}")
    except Exception:
        pass
    try:
        result = TOOL_MAP[name](**arguments)
        # Phase 1b: quarantine external tool results before they enter model context
        if name in ("search_web", "open_site", "ask_chatgpt", "ask_ai", "search_chatgpt_history", "open_chatgpt_conversation", "search_vault", "search_vault_semantic", "read_vault_note", "recall_facts", "search_sessions", "web_browse", "get_memory_context"):
            result = _quarantine_external(result, label=f"tool:{name}")
        # Also quarantine any result that looks like injection regardless of tool
        else:
            try:
                from cygnus.browser import guard
                if guard.is_suspicious(result):
                    result = guard.sanitize_web_text(result, label=f"tool:{name}")
            except Exception:
                pass
    except Exception as e:
        result = f"[Tool Error] {name}: {str(e)}"
        dur = time.monotonic() - start
        print(f"[TOOL] {name} raised {type(e).__name__} after {dur:.1f}s", flush=True)
        _audit_log(f"execute_tool:{name}", json.dumps(arguments, default=str)[:500], "error", result=result, confirm=confirmed, extra={"duration_ms": int(dur*1000)})
        return result
    dur = time.monotonic() - start
    print(f"[TOOL] {name} done in {dur:.1f}s", flush=True)
    # An app JARVIS just opened can be read in under a second: learn its
    # buttons in the background (at most once a day per app).
    if name == "open_application" and not str(result).startswith("[Error]"):
        try:
            from cygnus.apps import abilities as app_abilities
            app_abilities.scan_soon(arguments.get("app"))
        except Exception:
            pass
    _audit_log(f"execute_tool:{name}", json.dumps(arguments, default=str)[:500], "executed", result=result, confirm=confirmed, extra={"duration_ms": int(dur*1000)})
    # _audit_log is this call's one line in logs/audit.jsonl. An audit.log_call here
    # always raised (it has no decision= parameter) and was swallowed; made to
    # work it would have written every call twice (2026-09-22).
    safety.note_asked(name, result)
    return result
