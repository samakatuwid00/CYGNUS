"""cygnus.mcp.gitinfo - read-only git for a connected project: branch, status,
recent commits, diffs (docs/CONNECTOR.md).

Nothing here changes a repository. Every call is `git -C <folder> ...` with a
timeout; a folder that is not a repository, or a machine without git, comes
back as empty results rather than an exception. Secrets files are left out of
diffs the same way scanner leaves them out of reads.
"""

from __future__ import annotations

import os
import re
import subprocess

from cygnus.mcp import scanner

GIT_TIMEOUT_S = 15
DIFF_MAX = 60_000            # characters of diff returned before it is cut

# Pathspecs that keep secrets files out of a diff (git pathspec magic).
_EXCLUDE = [":(exclude,glob,icase)**/" + pat for pat in
            (".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*",
             "id_ed25519*", "id_ecdsa*", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
             "*credentials*", "secrets", "secrets.*", "service-account*.json")]
# A revision the caller may name: a hash, HEAD~n, or a branch-like name. No
# leading "-", so it can never be read as an option.
_REV_RE = re.compile(r"^(?:[0-9a-fA-F]{4,40}|HEAD(?:[~^]\d{0,3})*|[A-Za-z0-9][\w./-]{0,99})$")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(folder, *args, timeout: int = GIT_TIMEOUT_S) -> tuple[int, str]:
    """(returncode, stdout) of one git command in `folder`; (-1, "") when git
    is missing or hangs."""
    cmd = ["git", "-C", str(folder), "-c", "core.quotepath=off", "--no-pager", *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout, creationflags=_NO_WINDOW,
                             env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0"))
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return out.returncode, out.stdout or ""


def valid_rev(rev: str) -> bool:
    return bool(rev) and bool(_REV_RE.match(rev)) and ".." not in rev


def is_repo(folder) -> bool:
    code, out = _git(folder, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def branch(folder) -> str:
    code, out = _git(folder, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if code == 0 else ""


def head(folder) -> str:
    code, out = _git(folder, "rev-parse", "--short", "HEAD")
    return out.strip() if code == 0 else ""


# What the two porcelain letters mean, said plainly.
_STATE = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied",
          "U": "conflicted", "?": "untracked", "T": "type changed"}


def status(folder, limit: int = 200) -> list[dict]:
    """Uncommitted changes: [{"state", "path", "staged"}], secrets left out."""
    code, out = _git(folder, "status", "--porcelain=v1", "--untracked-files=normal")
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        x, y, path = line[0], line[1], line[3:]
        if " -> " in path:                       # a rename: show where it went
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if scanner.is_blocked(path):
            continue
        letter = x if x not in (" ", "?") else y
        rows.append({"state": _STATE.get(letter, letter), "path": path,
                     "staged": x not in (" ", "?")})
        if len(rows) >= limit:
            break
    return rows


def recent_commits(folder, limit: int = 10, files: bool = False) -> list[dict]:
    """The last `limit` commits, newest first: {"hash", "subject", "author",
    "when", "date"} and, with `files`, the paths each one touched."""
    limit = max(1, min(int(limit or 10), 100))
    fmt = "%x1e%h%x1f%s%x1f%an%x1f%ar%x1f%aI"
    args = ["log", f"-n{limit}", f"--format={fmt}"]
    if files:
        args.append("--name-status")
    code, out = _git(folder, *args)
    if code != 0:
        return []
    commits = []
    for block in out.split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        first, _, rest = block.partition("\n")
        parts = first.split("\x1f")
        if len(parts) < 5:
            continue
        c = {"hash": parts[0], "subject": parts[1], "author": parts[2], "when": parts[3],
             "date": parts[4]}
        if files:
            changed = []
            for line in rest.splitlines():
                bits = line.split("\t")
                if len(bits) >= 2 and not scanner.is_blocked(bits[-1]):
                    changed.append({"state": _STATE.get(bits[0][:1], bits[0]), "path": bits[-1]})
            c["files"] = changed
        commits.append(c)
    return commits


def diff(folder, path: str = "", since: str = "", staged: bool = False,
         max_chars: int = DIFF_MAX) -> dict:
    """The diff of uncommitted work (or the staged part, or everything since
    commit `since`), optionally for one path. {"diff", "truncated", "base",
    "files"}; raises ValueError for a bad revision, scanner.OutsideProject /
    Blocked for a bad path."""
    args = ["diff", "--no-color", "--no-ext-diff"]
    base = "working tree vs HEAD"
    if staged:
        args.append("--cached")
        base = "staged vs HEAD"
    if since:
        if not valid_rev(since):
            raise ValueError(f"{since!r} is not a commit.")
        code, _ = _git(folder, "rev-parse", "--verify", "--quiet", since + "^{commit}")
        if code != 0:
            raise ValueError(f"{since} is not a commit in this repository.")
        args.append(since)
        base = f"working tree vs {since}"
    spec = []
    if path:
        target = scanner.inside(folder, path)
        spec = [str(target)]
    code, names = _git(folder, *args, "--name-only", "--", *(spec or ["."]), *_EXCLUDE)
    code2, text = _git(folder, *args, "--", *(spec or ["."]), *_EXCLUDE)
    if code2 != 0:
        return {"diff": "", "truncated": False, "base": base, "files": []}
    max_chars = max(1000, int(max_chars or DIFF_MAX))
    return {"diff": text[:max_chars], "truncated": len(text) > max_chars, "base": base,
            "files": [n for n in names.splitlines() if n.strip()] if code == 0 else []}


def summary(folder) -> dict:
    """Branch, head, uncommitted files and the last few commits, in one call."""
    if not is_repo(folder):
        return {"git": False, "branch": "", "head": "", "status": [], "recent": []}
    return {"git": True, "branch": branch(folder), "head": head(folder),
            "status": status(folder), "recent": recent_commits(folder, 8)}
