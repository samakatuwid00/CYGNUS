"""prod_check.py - the routine check: is Cygnus fit to use right now?

One command runs every check in order and writes one report:

  1. compile   every Python file under the repo compiles
  2. tests     the pytest suite
  3. routing   eval/gate_routing.py, the deterministic routing gate
  4. server    a Cygnus server answers, 9router is up, and cloud models are
               healthy (a brain on the local model is a failure)
  5. smoke     scripts/cygnus_smoke.py against that server

  python scripts/prod_check.py                 safe smoke tier (nothing on the machine changes)
  python scripts/prod_check.py --tier live     also opens/closes Notepad and Calculator,
                                               opens two browser tabs, plays and pauses music
  python scripts/prod_check.py --no-server     only 1-3 (no running server needed)

Exit code 0 only when every check that ran passed. The report goes to
data/logs/prod-check-<time>.json. Run it before a push, after a restructure step,
and on a schedule (see the README's "Routine check").
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, REPO)
from cygnus import paths  # noqa: E402

# Fewer healthy cloud models than this and one quota reset leaves Cygnus on
# the local model (2026-09-22: 16 answered, 60% of 9router's list was dead).
MIN_HEALTHY_MODELS = 3


def _run(name, cmd, timeout):
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        ok, out = p.returncode == 0, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        ok, out = False, f"timed out after {timeout}s"
    lines = out.splitlines()
    # The summary line ("1066 passed", "13 of 13 steps passed", "GATE PASSED")
    # last, even when a background thread printed after it; errors only when
    # there is no summary.
    summary = next((l for pat in (r"\d+ (passed|failed)|steps passed|GATE", r"error")
                    for l in reversed(lines) if re.search(pat, l, re.I)), "")
    tail = [l for l in lines[-6:] if l != summary] + ([summary] if summary else [])
    result = {"check": name, "ok": ok, "seconds": round(time.monotonic() - start, 1), "detail": "\n".join(tail)}
    if not ok:
        result["output"] = "\n".join(lines[-80:])      # enough to see why, in the report
    return result


def check_compile():
    return _run("compile", [PY, "-m", "compileall", "-q", "-x", r"[\\/](\.venv|\.venv\.[^\\/]*|node_modules|\.recovery|\.patchtmp)[\\/]", "."], 300)


def check_tests():
    return _run("tests", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], 900)


def check_routing():
    return _run("routing", [PY, os.path.join("eval", "gate_routing.py")], 300)


def check_server(url):
    import httpx
    import cygnus_client
    start = time.monotonic()
    tried = [url] if url else list(cygnus_client.candidates())
    base = next((b for b in tried if cygnus_client._is_cygnus(b)), None)
    if not base:
        return {"check": "server", "ok": False, "seconds": 0.0,
                "detail": f"no Cygnus server on {' or '.join(tried)}; start it (desktop app or jarvis_web.py)"}, None
    st = httpx.get(base.rstrip("/") + "/status", timeout=5).json()
    problems = []
    from cygnus.brain import router_keeper
    if st.get("router_up") is False:
        problems.append("9router is not running")
    elif not router_keeper.responds(timeout=10):
        # The port can be open while 9router answers nothing (2026-09-22).
        problems.append("9router's port is open but it does not answer")
    models = st.get("router_models")
    if models is not None and len(models) < MIN_HEALTHY_MODELS:
        problems.append(f"only {len(models)} healthy 9router models ({', '.join(models) or 'none'})")
    if "ollama" in str(st.get("last_backend") or "").lower():
        problems.append("the last turn was answered by the local model")
    detail = (f"{base}: brain {st.get('brain')}, router_up {st.get('router_up')}, "
              f"{len(models or [])} healthy models, restarts {st.get('router_restarts')}")
    if problems:
        detail += " | " + "; ".join(problems)
    return {"check": "server", "ok": not problems, "seconds": round(time.monotonic() - start, 1),
            "detail": detail}, base


def check_smoke(base, tier):
    return _run(f"smoke ({tier})", [PY, os.path.join("scripts", "cygnus_smoke.py"), "--url", base,
                                     "--tier", tier], 1800)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tier", choices=("safe", "live", "agent"), default="safe")
    ap.add_argument("--url", help="server base URL (default: JARVIS_PORT, then 8001, then 8000)")
    ap.add_argument("--no-server", action="store_true", help="skip the server and smoke checks")
    args = ap.parse_args(argv)
    # Run by the daily scheduled task through pythonw: no console to print to.
    if sys.stdout is None:
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        sys.stdout = open(paths.log("prod-check-last.txt"), "w", encoding="utf-8")
    # The Windows console is cp1252; a reply with a curly quote crashed the report.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    results = []

    def record(r):
        results.append(r)
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['check']:<14} {r['seconds']:>6}s  "
              f"{r['detail'].splitlines()[-1] if r['detail'] else ''}", flush=True)
        if not r["ok"] and r["detail"]:
            for line in r["detail"].splitlines()[:-1]:
                print(f"        {line}")

    print("Cygnus prod check", flush=True)
    record(check_compile())
    record(check_tests())
    record(check_routing())
    if not args.no_server:
        r, base = check_server(args.url)
        record(r)
        if base:
            record(check_smoke(base, args.tier))

    os.makedirs(paths.LOG_DIR, exist_ok=True)
    path = paths.log(f"prod-check-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "tier": args.tier, "results": results},
                  f, indent=2, ensure_ascii=False)
    failed = [r["check"] for r in results if not r["ok"]]
    print(f"\n{'ALL PASSED' if not failed else 'FAILED: ' + ', '.join(failed)}; report: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
