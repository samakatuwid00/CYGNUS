"""chaos.py - what Cygnus does when the models fail.

Each scenario starts its own Cygnus (JARVIS_NO_VOICE=1, its own port, its own
data folder) pointed at a stub endpoint this script controls, so the failures
are exact and repeatable instead of waiting for a real quota to run out:

  healthy      the stub answers normally                -> Cygnus answers
  rate_limited every call is 429 "reset after 5m"        -> says so, no claim
  empty        200 with no content and no tool call      -> not an answer
  hangs        the endpoint never replies                -> hands over in time
  refused      nothing listens on the port at all        -> falls back, says why

What is measured, per scenario: did a reply arrive, how long it took, and
whether the reply claimed an action that never ran (the dangerous failure).

  python scripts/bench/chaos.py                  all scenarios
  python scripts/bench/chaos.py --only hangs
  python scripts/bench/chaos.py --keep-local     allow the local Ollama fallback

The local model is off by default: these scenarios are about the cloud path,
and a slow local model hides what the cloud path did.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from cygnus import paths  # noqa: E402
import cygnus_client as cc  # noqa: E402

# A reply that claims something happened. If one of these appears while the
# audit log shows no tool ran, Cygnus lied about acting - the failure that
# matters most here.
CLAIM_RE = re.compile(r"\b(i (?:opened|started|sent|closed|played|ran|wrote|deleted|stopped)|"
                      r"opened|playing|sent to|done[.,!]|i've (?:opened|sent|started))\b", re.I)
ASK = "what is the capital of the Philippines?"
TURN_LIMIT_S = 90.0


class Stub(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint that fails the way we ask it to."""

    mode = "healthy"

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"data": [{"id": "stub/model"}]})
        return self._json(404, {"error": "no"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        mode = Stub.mode
        if mode == "rate_limited":
            return self._json(503, {"error": {"message": "[stub/model] [429]: The usage limit has "
                                                         "been reached (reset after 5m)"}})
        if mode == "empty":
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": ""}}]})
        if mode == "hangs":
            time.sleep(TURN_LIMIT_S * 2)
            return
        return self._json(200, {"choices": [{"message": {"role": "assistant",
                                                         "content": "Manila, sir."}}]})


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_stub(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_cygnus(port, base_url, keep_local, data_dir, log_path):
    env = dict(os.environ)
    env.update(JARVIS_NO_VOICE="1", JARVIS_PORT=str(port), JARVIS_ROUTER_KEEP="0",
               ROUTER_BASE_URL=base_url, ROUTER_MODEL="stub/model", ROUTER_FALLBACK_MODELS="",
               ROUTER_API_KEY="stub", CYGNUS_DATA_DIR=data_dir,
               JARVIS_ROUTER_PROBE="0",           # no background probing of the stub
               JARVIS_USE_OLLAMA="true" if keep_local else "false",
               JARVIS_TURN_BUDGET="45", JARVIS_FIRST_CALL_S="20")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "cygnus"], cwd=REPO, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"Cygnus exited while starting; see {log_path}")
        if cc._is_cygnus(base):
            return proc, base
        time.sleep(1)
    proc.kill()
    raise SystemExit(f"Cygnus did not start in 180 s; see {log_path}")


def one_turn(base, say, audit_path):
    """(reply, seconds, tools) for one typed turn. The audit log is this
    scenario's own (each Cygnus has its own data folder)."""
    import asyncio

    async def go():
        async with cc.connect(base) as ws:
            await cc.hello(ws, "bench-chaos")
            mark = os.path.getsize(audit_path) if os.path.exists(audit_path) else 0
            start = time.monotonic()
            turn = await cc.ask(ws, say, timeout=TURN_LIMIT_S)
            return turn, round(time.monotonic() - start, 1), cc.tools_since(mark, path=audit_path)
    return asyncio.run(go())


SCENARIOS = ["healthy", "rate_limited", "empty", "hangs", "refused"]


def run(scenario, keep_local, tmp):
    stub_port = free_port()
    srv = None
    if scenario == "refused":
        base_url = f"http://127.0.0.1:{free_port()}/v1"      # nothing listens
    else:
        Stub.mode = scenario
        srv = start_stub(stub_port)
        base_url = f"http://127.0.0.1:{stub_port}/v1"
    port = free_port()
    data_dir = os.path.join(tmp, scenario)
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    log_path = os.path.join(tmp, f"{scenario}-server.log")
    proc, base = start_cygnus(port, base_url, keep_local, data_dir, log_path)
    try:
        turn, seconds, tools = one_turn(base, ASK, os.path.join(data_dir, "logs", "audit.jsonl"))
        reply = (turn.reply or "").strip() if turn else ""
        answered = bool(reply)
        claimed = bool(CLAIM_RE.search(reply)) and not tools
        return {"scenario": scenario, "answered": answered, "seconds": seconds,
                "claimed_an_action_that_did_not_run": claimed, "tools": tools,
                "reply": reply[:300], "error": (turn.error if turn else "no turn")}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        if srv:
            srv.shutdown()


def verdicts(rows):
    out = []
    for r in rows:
        s = r["scenario"]
        if r["claimed_an_action_that_did_not_run"]:
            out.append(f"{s}: claimed an action that never ran - {r['reply'][:80]}")
        if s == "healthy" and not r["answered"]:
            out.append("healthy: the stub answered but Cygnus did not")
        if s != "healthy" and not r["answered"]:
            out.append(f"{s}: no reply at all within {TURN_LIMIT_S:.0f}s (it should say what went wrong)")
        if s == "hangs" and r["seconds"] > 60:
            out.append(f"hangs: took {r['seconds']}s to give up on a dead model")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="one scenario: " + ", ".join(SCENARIOS))
    ap.add_argument("--keep-local", action="store_true", help="allow the local Ollama fallback")
    args = ap.parse_args(argv)
    todo = [args.only] if args.only else SCENARIOS
    for s in todo:
        if s not in SCENARIOS:
            raise SystemExit(f"unknown scenario {s}; pick from {', '.join(SCENARIOS)}")
    tmp = tempfile.mkdtemp(prefix="cygnus-chaos-")
    rows = []
    print(f"chaos: {len(todo)} scenario(s), each its own Cygnus (no voice, no 9router)")
    try:
        for s in todo:
            r = run(s, args.keep_local, tmp)
            rows.append(r)
            print(f"  {'ok ' if r['answered'] and not r['claimed_an_action_that_did_not_run'] else 'BAD'}"
                  f"  {s:<13} {r['seconds']:>5}s  {r['reply'][:90]!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    found = verdicts(rows)
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    path = paths.log(f"bench-chaos-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": rows, "findings": found},
                  f, indent=2, ensure_ascii=False)
    print("\n" + ("\n".join("  - " + f for f in found) if found else "  nothing to report"))
    print(f"report: {path}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
