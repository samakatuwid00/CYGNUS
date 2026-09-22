"""migrate_data.py - move Cygnus's runtime data from the repository root
into data/ (docs/plans/RESTRUCTURE_PLAN.md, phase 4).

What moves: logs/, memory/, sessions/, models/, .warm_harness/,
.hermes-jobs/, and app_registry.json, web_registry.json, capabilities.json,
jarvis-profile.md, manus_cookies.json, projects.log.

Each item is renamed (same drive: instant, nothing is copied or rewritten).
It never merges or overwrites: an item that exists in both places stops the
run before anything moves. It refuses while Cygnus is running, since the
server writes to these folders.

  python scripts/migrate_data.py            show what would move
  python scripts/migrate_data.py --apply    move it

CYGNUS_DATA_DIR in the environment moves it there instead of data/.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("CYGNUS_DATA_DIR") or os.path.join(ROOT, "data")
ITEMS = ["logs", "memory", "sessions", "models", ".warm_harness", ".hermes-jobs",
         "app_registry.json", "web_registry.json", "capabilities.json", "jarvis-profile.md",
         "manus_cookies.json", "projects.log"]


def cygnus_running():
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:
            continue
        if p.pid == os.getpid():
            continue
        if ("-m cygnus" in cmd and "cygnus_" not in cmd) or "jarvis_web.py" in cmd \
                or ("electron" in (p.info.get("name") or "").lower() and "CYGNUS" in cmd):
            found.append(f"{p.pid} {cmd[:100]}")
    return found


def main(argv):
    apply = "--apply" in argv
    moves, clashes = [], []
    for name in ITEMS:
        src, dst = os.path.join(ROOT, name), os.path.join(DATA, name)
        if not os.path.exists(src):
            continue
        (clashes if os.path.exists(dst) else moves).append((src, dst))
    print(f"data folder: {DATA}")
    for src, dst in moves:
        print(f"  move  {os.path.relpath(src, ROOT)}")
    for src, dst in clashes:
        print(f"  CLASH {os.path.relpath(src, ROOT)} exists in both places")
    if clashes:
        print("Nothing moved: sort out the clashes first (this script never merges).")
        return 1
    if not moves:
        print("Nothing to move.")
        return 0
    if not apply:
        print("Dry run. Re-run with --apply.")
        return 0
    running = cygnus_running()
    if running:
        print("Cygnus is running; quit it first:\n  " + "\n  ".join(running))
        return 1
    os.makedirs(DATA, exist_ok=True)
    for src, dst in moves:
        os.replace(src, dst)
        print(f"  moved {os.path.relpath(src, ROOT)}")
    left = [os.path.relpath(s, ROOT) for s, _ in moves if os.path.exists(s)]
    missing = [os.path.relpath(d, ROOT) for _, d in moves if not os.path.exists(d)]
    if left or missing:
        print(f"Check by hand: still in the root {left}, missing in data {missing}")
        return 1
    print(f"Moved {len(moves)} item(s) into {DATA}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
