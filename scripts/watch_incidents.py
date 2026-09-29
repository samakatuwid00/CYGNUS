"""watch_incidents.py - follow what Cygnus does and write down what goes wrong.

Follows two files and one URL:
  data/logs/turns.jsonl          one line per turn (cygnus/incidents.py record_turn)
  data/logs/desktop-backend.log  the backend's output, when the desktop app owns it
  http://127.0.0.1:<JARVIS_PORT>/status   is the backend there at all

Each problem (cygnus/incidents.py says which) becomes one line in
data/logs/incidents.jsonl, with the turn and the log lines before it. An error
is also a Windows notification; a warning (a slow turn, a HUD that went away
mid-reply) is only written down. The same error again within ten minutes is
counted in the file but not announced again.

  python scripts/watch_incidents.py                 follow from now on (Ctrl+C stops)
  python scripts/watch_incidents.py --from-start    read both files from the top first
  python scripts/watch_incidents.py --once          read what is new, then exit
  python scripts/watch_incidents.py --summary       what went wrong in the last day
  python scripts/watch_incidents.py --no-toast      write it down, show nothing

Outliving the terminal (Task Scheduler owns it; see run_detached.ps1):
  powershell -ExecutionPolicy Bypass -File scripts\\windows\\run_detached.ps1 `
      -Name CygnusIncidentWatch -Script scripts\\watch_incidents.py

Nothing leaves the machine: the files are read here and written here.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from cygnus import incidents, paths  # noqa: E402

BACKEND_LOG = paths.log("desktop-backend.log")
STATE_PATH = paths.log("incident_watch_state.json")
PID_PATH = paths.log("incident_watch.pid")

TOAST_AGAIN_S = 600          # the same problem is announced at most this often
HEALTH_EVERY_S = 30          # how often /status is asked
DOWN_AFTER_S = 60            # unanswered this long is an incident, not a restart
CONTEXT_LINES = 12


class Follower:
    """New complete lines of a file that grows; starts over when it shrinks
    (a new log after a restart)."""

    def __init__(self, path: str, offset: int | None = None):
        self.path = path
        self.offset = offset
        self._partial = b""

    def read(self) -> list[str]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if self.offset is None:
            self.offset = size
        if size < self.offset:
            self.offset, self._partial = 0, b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        self.offset = size
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()             # the last one may still be being written
        return [ln.decode("utf-8", "replace").rstrip("\r") for ln in lines]


def toast(title: str, body: str) -> None:
    """A Windows notification, through PowerShell's WinRT access; never waits
    and never fails the watcher."""
    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "''"))
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title[:80])}</text>"
        f"<text>{esc(body[:200])}</text></binding></visual></toast>');"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show("
        "[Windows.UI.Notifications.ToastNotification]::new($x))"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


_TITLES = {
    "turn_crashed": "a turn crashed", "no_model": "no model answered",
    "empty_reply": "an empty reply", "error_reply": "an error shown as the reply",
    "model_stopped": "the model stopped mid-turn", "slow_turn": "a slow turn",
    "traceback": "an exception in the backend", "agent_crashed": "the agent loop crashed",
    "server_error": "a server error", "websocket_error": "a connection to the HUD failed",
    "observer_failed": "a turn observer failed", "confirm_failed": "a confirm failed",
    "backend_down": "the backend is not answering",
}


class Watcher:
    def __init__(self, notify=toast, now=time.time, incidents_path: str | None = None):
        self.notify = notify
        self.now = now
        self.incidents_path = incidents_path or incidents.INCIDENTS_PATH
        self.scanner = incidents.LogScanner()
        self.context = collections.deque(maxlen=40)
        self.announced: dict[str, float] = {}
        self.repeats: collections.Counter = collections.Counter()
        self.found = 0

    def log_lines(self, lines):
        for line in lines:
            if not line.strip() or incidents.NOISE_RE.search(line):
                continue
            for p in self.scanner.feed(line):
                self._report(p, "log")
            self.context.append(line[:300])

    def turn_lines(self, lines):
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            for p in incidents.turn_problems(rec):
                self._report(p, "turn", turn=rec)

    def backend_down(self, seconds: float, url: str):
        self._report({"kind": "backend_down", "severity": "error",
                      "detail": f"{url} unanswered for {seconds:.0f}s"}, "health")

    def _report(self, problem: dict, source: str, turn: dict | None = None):
        fp = incidents.fingerprint(problem)
        now = self.now()
        self.repeats[fp] += 1
        rec = {"ts": datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds"),
               "source": source, **{k: v for k, v in problem.items()}, "repeat": self.repeats[fp]}
        if turn is not None:
            rec["turn"] = {k: turn.get(k) for k in ("ts", "client", "user", "reply", "backend",
                                                     "model", "seconds", "broke", "tools", "error")}
        rec["context"] = list(self.context)[-CONTEXT_LINES:]
        try:
            os.makedirs(os.path.dirname(self.incidents_path), exist_ok=True)
            with open(self.incidents_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[watch] could not write {self.incidents_path}: {e}", flush=True)
        self.found += 1
        what = _TITLES.get(problem["kind"], problem["kind"])
        said = f" - “{turn.get('user', '')[:60]}”" if turn else ""
        print(f"[watch] {rec['ts']} {problem['severity'].upper()} {what}{said}: {problem.get('detail', '')[:160]}",
              flush=True)
        if problem["severity"] == "error" and now - self.announced.get(fp, 0) >= TOAST_AGAIN_S:
            self.announced[fp] = now
            self.notify(f"Cygnus: {what}", (said.lstrip(" -") + " " if said else "") + problem.get("detail", "")[:160])


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(followers):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({fl.path: fl.offset for fl in followers}, f)
    except OSError:
        pass


def _already_running() -> int | None:
    try:
        import psutil
        with open(PID_PATH, encoding="utf-8") as f:
            pid = int(f.read().strip())
        if pid != os.getpid() and psutil.pid_exists(pid) and \
                "watch_incidents" in " ".join(psutil.Process(pid).cmdline()):
            return pid
    except Exception:
        pass
    return None


def _status_url():
    import cygnus_client
    return f"http://127.0.0.1:{cygnus_client._configured_port() or 8000}/status"


def summary(hours: float = 24.0) -> None:
    since = time.time() - hours * 3600
    counts, last = collections.Counter(), {}
    try:
        with open(incidents.INCIDENTS_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    ts = datetime.datetime.fromisoformat(rec["ts"]).timestamp()
                except (ValueError, KeyError):
                    continue
                if ts >= since:
                    counts[rec["kind"]] += 1
                    last[rec["kind"]] = rec
    except OSError:
        pass
    if not counts:
        print(f"No incidents in the last {hours:.0f}h.")
        return
    print(f"Incidents in the last {hours:.0f}h ({incidents.INCIDENTS_PATH}):")
    for kind, n in counts.most_common():
        rec = last[kind]
        said = f" “{rec['turn'].get('user', '')[:50]}”" if rec.get("turn") else ""
        print(f"  {n:4d}  {_TITLES.get(kind, kind):32s} last {rec['ts'][11:16]}{said}: {rec.get('detail', '')[:80]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-start", action="store_true", help="read both files from the top first")
    ap.add_argument("--once", action="store_true", help="read what is new, then exit")
    ap.add_argument("--summary", action="store_true", help="what went wrong in the last day")
    ap.add_argument("--no-toast", action="store_true", help="write incidents down, show nothing")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between reads")
    args = ap.parse_args()

    if args.summary:
        return summary()
    other = _already_running()
    if other and not args.once:
        raise SystemExit(f"A watcher is already running (pid {other}).")
    if not args.once:
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    state = {} if args.from_start else _load_state()
    start = 0 if args.from_start else None
    followers = [Follower(incidents.TURNS_PATH, state.get(incidents.TURNS_PATH, start)),
                 Follower(BACKEND_LOG, state.get(BACKEND_LOG, start))]
    turns, backend = followers
    watcher = Watcher(notify=(lambda t, b: None) if args.no_toast else toast)
    url = _status_url()
    print(f"[watch] following {turns.path} and {backend.path}; asking {url}; "
          f"incidents go to {watcher.incidents_path}", flush=True)

    import httpx
    last_health, down_since, reported_down = 0.0, None, False
    try:
        while True:
            watcher.log_lines(backend.read())     # the log first: a turn's context is what came before it
            watcher.turn_lines(turns.read())
            _save_state(followers)
            if args.once:
                break
            if time.time() - last_health >= HEALTH_EVERY_S:
                last_health = time.time()
                try:
                    ok = httpx.get(url, timeout=5).status_code == 200
                except httpx.HTTPError:
                    ok = False
                if ok:
                    if reported_down:
                        print(f"[watch] the backend answers again after {time.time() - down_since:.0f}s", flush=True)
                    down_since, reported_down = None, False
                else:
                    down_since = down_since or time.time()
                    if not reported_down and time.time() - down_since >= DOWN_AFTER_S:
                        watcher.backend_down(time.time() - down_since, url)
                        reported_down = True
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        if not args.once:
            try:
                os.remove(PID_PATH)
            except OSError:
                pass
    print(f"[watch] {watcher.found} incident(s) written.", flush=True)


if __name__ == "__main__":
    main()
