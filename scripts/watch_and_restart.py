"""Watch Cygnus's Python files and restart the server (python -m cygnus) when
one changes. Usage: python scripts/watch_and_restart.py
Polls every 2s. Kills old process, starts new one. Ctrl+C to stop.
Do not run it while the desktop app owns the backend: both would start a
server on the same port."""
import glob
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the repository
from cygnus import paths  # noqa: E402

ROOT = str(paths.ROOT)
WATCH = sorted(glob.glob(os.path.join(ROOT, "cygnus", "**", "*.py"), recursive=True))
POLL = 2  # seconds

mtimes = {f: os.path.getmtime(f) for f in WATCH if os.path.exists(f)}
proc = None

def start():
    global proc
    print("[watcher] Starting python -m cygnus ...", flush=True)
    proc = subprocess.Popen([sys.executable, "-m", "cygnus"], cwd=ROOT)
    print(f"[watcher] PID {proc.pid}", flush=True)

def stop():
    global proc
    if proc and proc.poll() is None:
        print(f"[watcher] Stopping PID {proc.pid} ...", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print(f"[watcher] Stopped.", flush=True)
    proc = None

def check():
    changed = False
    for f in WATCH:
        if not os.path.exists(f):
            continue
        t = os.path.getmtime(f)
        if t != mtimes.get(f):
            print(f"[watcher] Changed: {os.path.basename(f)}", flush=True)
            mtimes[f] = t
            changed = True
    return changed

signal.signal(signal.SIGINT, lambda *_: (stop(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))

start()
print(f"[watcher] Watching {len(WATCH)} files, poll={POLL}s. Ctrl+C to stop.", flush=True)

while True:
    time.sleep(POLL)
    if proc and proc.poll() is not None:
        print(f"[watcher] Process exited (code={proc.returncode}), restarting ...", flush=True)
        start()
    elif check():
        # Don't kill a live autonomous job — Hermes is mid-run in a subprocess
        # we can't hand off. Wait until it finishes (or is cancelled) instead.
        try:
            from cygnus.jobs import autonomous
            if autonomous.any_running():
                print("[watcher] Deferring restart — autonomous job running. "
                      "Will restart after it finishes.", flush=True)
                continue
        except Exception:
            pass
        stop()
        start()
