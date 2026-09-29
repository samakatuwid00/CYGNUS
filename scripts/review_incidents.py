"""review_incidents.py - a model reads the turns since the last review and says
which ones did not do what was asked.

The incident watcher (watch_incidents.py) catches the failures that leave a
mark: no model answered, an exception, an error shown as the reply. It cannot
see a turn that went wrong while looking right. "play woman by harry styles"
clicked Harry Styles's cover of "Juice" and replied that it had played it
(2026-09-29); every log line said success.

So once an hour (install_incident_tasks.ps1) the new turns go to Claude through
Claude Code (`claude -p`, no tools, no hooks, no session kept), and what it
finds is added to data/logs/incidents.jsonl beside the watcher's findings, with
one Windows notification per review when something is wrong.

What leaves the machine: the new turns' words and replies (600 characters
each at most), from the desktop app, a browser tab and the phone. Turns from
test scripts (client "session:...") are not sent. docs/PRIVACY.md says so too.

  python scripts/review_incidents.py              review what is new since the last review
  python scripts/review_incidents.py --dry-run    print what would be sent, send nothing
  python scripts/review_incidents.py --model sonnet
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from cygnus import incidents, paths  # noqa: E402

STATE_PATH = paths.log("incident_review_state.json")
MAX_TURNS = 60              # a review past a long absence looks at the latest ones
TEXT_CAP = 300
CALL_TIMEOUT_S = 240

KINDS = ["wrong_action", "false_claim", "misunderstood", "unhelpful", "other"]
SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "turn": {"type": "integer", "description": "the number of the turn in the list"},
                "kind": {"type": "string", "enum": KINDS},
                "severity": {"type": "string", "enum": ["error", "warning"]},
                "why": {"type": "string", "description": "one sentence: what was asked, what happened instead"},
            },
            "required": ["turn", "kind", "severity", "why"]}},
    },
    "required": ["findings"],
}

SYSTEM = """You review turns of Cygnus, a voice assistant on a Windows laptop. Each turn \
is what the owner said, what Cygnus replied, which backend answered and which tools ran. \
The owner speaks English and Tagalog/Taglish; speech-to-text sometimes mishears.

Report only turns where Cygnus went wrong while the log would call it a success:
- wrong_action: it did something other than what was asked (another song, app, file, site).
- false_claim: the reply says something was done or is true that the tools listed do not support.
- misunderstood: it answered a different question than the one asked.
- unhelpful: it asked for clarification or refused when the request was clear, or looped.
- other: a real problem that fits none of these.

Do not report: a turn already marked FLAGGED (a rule caught it), model outages, a turn \
that asks a sensible clarifying question, style or tone, or anything you are unsure of. \
severity is error when something wrong was done or claimed, warning otherwise. \
An empty findings list is the normal answer."""


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def new_turns(path: str, offset: int) -> tuple[list[dict], int]:
    """The owner's turns after `offset`, and where the file ends now."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    if size < offset:                   # the log was deleted and started over
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(size - offset)
    end = offset + data.rfind(b"\n") + 1 if b"\n" in data else offset
    turns = []
    for line in data[: end - offset].decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not str(rec.get("client") or "").startswith("session:"):
            turns.append(rec)
    return turns[-MAX_TURNS:], end


def prompt_for(turns: list[dict]) -> str:
    lines = [f"{len(turns)} turns, oldest first:\n"]
    for i, t in enumerate(turns, 1):
        flagged = incidents.turn_problems(t)
        mark = f"  FLAGGED: {', '.join(p['kind'] for p in flagged)}" if flagged else ""
        lines.append(
            f"[{i}] {t.get('ts', '')} via {t.get('client')}{mark}\n"
            f"  owner: {str(t.get('user') or '')[:TEXT_CAP]}\n"
            f"  cygnus: {str(t.get('reply') or '')[:TEXT_CAP]}\n"
            f"  backend: {t.get('backend')}  model: {t.get('model')}  {t.get('seconds')}s  "
            f"tools: {', '.join(t.get('tools') or []) or 'none'}")
    return "\n".join(lines)


def ask_claude(prompt: str, model: str) -> list[dict]:
    exe = shutil.which("claude") or os.path.expanduser(r"~\.local\bin\claude.exe")
    cmd = [exe, "-p", "--model", model, "--tools", "", "--no-session-persistence",
           "--settings", json.dumps({"disableAllHooks": True}),
           "--system-prompt", SYSTEM,
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
    # A folder outside the repo, with no CLAUDE.md or git of its own: the
    # review sees the turns and nothing of the code.
    import tempfile
    cwd = os.path.join(tempfile.gettempdir(), "cygnus-review")
    os.makedirs(cwd, exist_ok=True)
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                          timeout=CALL_TIMEOUT_S, cwd=cwd,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude -p: {str(out.get('result'))[:300]}")
    return (out.get("structured_output") or {}).get("findings") or []


def record(findings: list[dict], turns: list[dict], path: str | None = None) -> list[dict]:
    """Findings that name a real turn, written as incidents; returns them."""
    path = path or incidents.INCIDENTS_PATH
    now = datetime.datetime.now().isoformat(timespec="seconds")
    kept = []
    for f in findings:
        n = f.get("turn")
        if not isinstance(n, int) or not 1 <= n <= len(turns):
            continue
        t = turns[n - 1]
        rec = {"ts": now, "source": "review", "kind": "review_" + str(f.get("kind", "other")),
               "severity": "error" if f.get("severity") == "error" else "warning",
               "detail": str(f.get("why", ""))[:300], "repeat": 1,
               "turn": {k: t.get(k) for k in ("ts", "client", "user", "reply", "backend",
                                                "model", "seconds", "broke", "tools", "error")}}
        kept.append(rec)
    if kept:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for rec in kept:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return kept


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print what would be sent, send nothing")
    ap.add_argument("--model", default=os.getenv("CYGNUS_REVIEW_MODEL", "sonnet"))
    ap.add_argument("--no-toast", action="store_true")
    args = ap.parse_args()

    state = _load_state()
    turns, end = new_turns(incidents.TURNS_PATH, int(state.get("offset", 0)))
    if not turns:
        print("[review] no new turns from the owner.", flush=True)
        if not args.dry_run:
            _save_state({**state, "offset": end})
        return
    prompt = prompt_for(turns)
    if args.dry_run:
        print(prompt)
        return
    try:
        findings = ask_claude(prompt, args.model)
    except Exception as e:
        # The turns stay unreviewed and go with the next review.
        print(f"[review] failed: {e}", flush=True)
        raise SystemExit(1)
    kept = record(findings, turns)
    _save_state({"offset": end, "last": datetime.datetime.now().isoformat(timespec="seconds"),
                 "reviewed": len(turns), "found": len(kept)})
    print(f"[review] {len(turns)} turns, {len(kept)} finding(s).", flush=True)
    for rec in kept:
        print(f"  {rec['severity'].upper()} {rec['kind']}: “{rec['turn']['user'][:60]}” - {rec['detail']}")
    errors = [r for r in kept if r["severity"] == "error"]
    if errors and not args.no_toast:
        import watch_incidents
        first = errors[0]
        watch_incidents.toast(f"Cygnus review: {len(errors)} turn(s) went wrong",
                              f"“{first['turn']['user'][:50]}” - {first['detail']}")


if __name__ == "__main__":
    main()
