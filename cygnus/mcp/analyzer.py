"""cygnus.mcp.analyzer - a quick, read-only look at a connected project's code
(docs/CONNECTOR.md): languages and line counts, the biggest files, TODO
markers, test files; for one file, its definitions and imports.

Static only: nothing is imported, built or run. Python is read with `ast`;
other languages with a few patterns, which is enough to say what a file
holds, not to check it.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from cygnus.mcp import scanner

LANGUAGES = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue",
    ".svelte": "Svelte", ".php": "PHP", ".rb": "Ruby", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".swift": "Swift", ".dart": "Dart", ".cs": "C#",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".html": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell", ".md": "Markdown",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
}
LINE_COUNT_MAX = 1_000_000   # bytes; bigger files are counted, not read
FILE_LIMIT = 4000            # files read for a project analysis

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|[^/]+_test\.(py|go)$|"
                      r"\.(test|spec)\.[jt]sx?$")
_COMMENT = {"Python": "#", "Ruby": "#", "Shell": "#", "PowerShell": "#", "YAML": "#", "TOML": "#",
            "SQL": "--"}
# Definitions in languages without a parser here.
_DEF_RES = {
    "JavaScript": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
                             r"(?:function\s*\*?\s*(\w+)|class\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*"
                             r"(?:async\s+)?(?:function|\([^)]*\)\s*=>|\w+\s*=>))", re.M),
    "PHP": re.compile(r"^\s*(?:(?:public|private|protected|static|abstract|final)\s+)*"
                      r"(?:function\s+(\w+)|class\s+(\w+)|interface\s+(\w+)|trait\s+(\w+))", re.M),
    "Go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)|^type\s+(\w+)\s+(?:struct|interface)", re.M),
    "Rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn\s+(\w+)|struct\s+(\w+)|"
                       r"enum\s+(\w+)|trait\s+(\w+))", re.M),
}
_DEF_RES["TypeScript"] = _DEF_RES["JavaScript"]
_IMPORT_RES = {
    "JavaScript": re.compile(r"""^\s*import\s[^'"]*['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)""", re.M),
    "PHP": re.compile(r"^\s*use\s+([\w\\]+)", re.M),
    "Go": re.compile(r'^\s*(?:import\s+)?"([\w./-]+)"', re.M),
    "Rust": re.compile(r"^\s*use\s+([\w:]+)", re.M),
}
_IMPORT_RES["TypeScript"] = _IMPORT_RES["JavaScript"]


def language(path) -> str:
    return LANGUAGES.get(os.path.splitext(str(path))[1].lower(), "")


def _count(text: str, lang: str) -> dict:
    lines = text.splitlines()
    blank = sum(1 for l in lines if not l.strip())
    mark = _COMMENT.get(lang, "//")
    comment = sum(1 for l in lines if l.strip().startswith(mark))
    return {"lines": len(lines), "blank": blank, "comment": comment,
            "code": len(lines) - blank - comment, "todos": len(_TODO_RE.findall(text))}


def _python_outline(text: str) -> dict:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as e:
        return {"parse_error": f"{type(e).__name__}: {e}"}
    funcs, classes, imports, longest = [], [], set(), []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(node.name)
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            longest.append((end - node.lineno + 1, node.name, node.lineno))
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    longest.sort(reverse=True)
    return {"functions": len(funcs), "classes": classes[:50], "imports": sorted(imports)[:60],
            "longest_functions": [{"name": n, "line": ln, "lines": size} for size, n, ln in longest[:5]],
            "docstring": bool(ast.get_docstring(tree))}


def _pattern_outline(text: str, lang: str) -> dict:
    out = {}
    if lang in _DEF_RES:
        names = [next(g for g in m.groups() if g) for m in _DEF_RES[lang].finditer(text)
                 if any(m.groups())]
        out["definitions"] = names[:80]
        out["definition_count"] = len(names)
    if lang in _IMPORT_RES:
        imps = {next(g for g in m.groups() if g) for m in _IMPORT_RES[lang].finditer(text)
                if any(m.groups())}
        out["imports"] = sorted(imps)[:60]
    return out


def analyze_file(folder, rel: str) -> dict:
    """One file: its language, line counts, TODOs and (where it can be read)
    its definitions and imports. Same guards as read_file."""
    got = scanner.read_text(folder, rel, max_bytes=LINE_COUNT_MAX)
    lang = language(got["path"]) or "text"
    out = {"path": got["path"], "language": lang, "bytes": got["size"],
           "partial": got["truncated"], **_count(got["text"], lang)}
    if lang == "Python":
        out.update(_python_outline(got["text"]))
    else:
        out.update(_pattern_outline(got["text"], lang))
    if got["flagged"]:
        out["instruction_like_lines"] = got["flagged"]
    return out


def analyze_project(folder) -> dict:
    """The whole project: lines by language, biggest files, TODO count, test
    files, stack. Reads at most FILE_LIMIT source files."""
    root = Path(folder).resolve()
    listing = scanner.scan(root, limit=scanner.SCAN_MAX)
    by_lang: dict = {}
    sizes, todo_files, tests, read = [], [], [], 0
    for f in listing["files"]:
        lang = language(f["path"])
        if _TEST_RE.search(f["path"]):
            tests.append(f["path"])
        if not lang or read >= FILE_LIMIT:
            continue
        row = by_lang.setdefault(lang, {"files": 0, "lines": 0, "code": 0})
        row["files"] += 1
        if f["size"] > LINE_COUNT_MAX:
            sizes.append((f["size"], f["path"], None))
            continue
        try:
            text = (root / f["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        read += 1
        c = _count(text, lang)
        row["lines"] += c["lines"]
        row["code"] += c["code"]
        sizes.append((f["size"], f["path"], c["lines"]))
        if c["todos"]:
            todo_files.append((c["todos"], f["path"]))
    sizes.sort(reverse=True)
    todo_files.sort(reverse=True)
    return {
        "folder": str(root).replace("\\", "/"),
        "stack": scanner.stack(root),
        "files": listing["total"],
        "source_files_read": read,
        "partial": listing["truncated"] or read >= FILE_LIMIT,
        "languages": dict(sorted(by_lang.items(), key=lambda kv: -kv[1]["lines"])),
        "largest_files": [{"path": p, "bytes": s, "lines": n} for s, p, n in sizes[:10]],
        "todos": sum(n for n, _ in todo_files),
        "todo_files": [{"path": p, "count": n} for n, p in todo_files[:10]],
        "test_files": len(tests),
        "test_examples": tests[:10],
    }
