"""soak.py - leave Cygnus running and watch for the slow failures.

A crash, a leak, a runaway job and a router that quietly dies do not show up
in a two-minute test. This one asks a little, often, for hours, and records
what it sees to data/logs/soak-<time>.jsonl (one line per check, so the file
is readable while it is still running):

  every --every seconds   one instant turn (time / maths / a greeting)
  every 10 checks         one recall turn (memory, sites, job status)
  every hour              one turn that needs the model
  every check             /status, and the backend process's memory and CPU

It counts: turns that failed or timed out, replies that claim an action the
audit log does not show, 9router restarts, and how the backend's memory
moves from its first reading.

  python scripts/bench/soak.py --hours 24
  python scripts/bench/soak.py --hours 1 --every 60
  python scripts/bench/soak.py --report data/logs/soak-20260923-101500.jsonl

Cheap on purpose: one model call an hour, the rest answered locally.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from cygnus import paths  # noqa: E402
import cygnus_client as cc  # noqa: E402

CLAIM_RE = re.compile(r"\b(i (?:opened|started|sent|closed|played|ran|wrote|deleted)|opened|playing)\b", re.I)
INSTANT = ["what time is it", "17 * 23", "hello cygnus", "anong oras na ngayon?"]
RECALL = ["what do you remember about me?", "what sites do you know?", "what's the status of the task?"]
MODEL = ["in one sentence, what is TCP?", "can you visit websites?"]


def backend_process():
    """The Cygnus server process, or None."""
    try:
        import psutil
    except ImportError:
        return None
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:
            continue
        if re.search(r"-m\s+cygnus(\s|$)", cmd) or "jarvis_web.py" in cmd:
            try:
                if p.memory_info().rss > 100 * 2 ** 20:      # the server, not its launcher
                    return p
            except Exception:
                continue
    return None


async def one_turn(base, say):
    async with cc.connect(base) as ws:
        await cc.hello(ws, "bench-soak")
        mark = cc.audit_mark()
        start = time.monotonic()
        turn = await cc.ask(ws, say, timeout=120)
        return turn, round(time.monotonic() - start, 2), cc.tools_since(mark)


def summarise(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if not rows:
        return "nothing recorded yet"
    checks = [r for r in rows if r.get("kind") == "check"]
    rss = [r["rss_mb"] for r in checks if r.get("rss_mb")]
    failed = [r for r in checks if not r.get("answered")]
    claims = [r for r in checks if r.get("unsupported_claim")]
    restarts = [r.get("router_restarts", 0) for r in checks if r.get("router_restarts") is not None]
    hours = round((checks[-1]["t"] - checks[0]["t"]) / 3600, 2) if len(checks) > 1 else 0
    gone = [r for r in checks if r.get("server") == "gone"]
    return (f"{len(checks)} checks over {hours} h\n"
            f"  turns that failed:        {len(failed)}\n"
            f"  unsupported claims:       {len(claims)}\n"
            f"  server missing at check:  {len(gone)}\n"
            f"  9router restarts:         {(max(restarts) - min(restarts)) if restarts else 'n/a'}\n"
            f"  memory first -> last:     {rss[0]:.0f} -> {rss[-1]:.0f} MB (peak {max(rss):.0f})"
            if rss else "  memory: not measured")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--every", type=float, default=300.0, help="seconds between checks")
    ap.add_argument("--report", help="summarise an earlier run and exit")
    args = ap.parse_args(argv)
    if args.report:
        print(summarise(args.report))
        return 0
    base = cc.find_server(args.url)
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    path = paths.log(f"soak-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    print(f"soak: {base} for {args.hours} h, a check every {args.every:.0f}s -> {path}", flush=True)
    end = time.time() + args.hours * 3600
    n = 0
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps({"kind": "start", "t": time.time(), "base": base,
                              "hours": args.hours, "every": args.every}) + "\n")
        out.flush()
        while time.time() < end:
            n += 1
            say = (MODEL[n % len(MODEL)] if n % max(1, int(3600 / args.every)) == 0
                   else RECALL[n % len(RECALL)] if n % 10 == 0 else INSTANT[n % len(INSTANT)])
            row = {"kind": "check", "n": n, "t": round(time.time(), 1), "say": say}
            try:
                turn, seconds, tools = asyncio.run(one_turn(base, say))
                reply = (turn.reply or "").strip() if turn else ""
                row.update(answered=bool(reply), seconds=seconds,
                           backend=(turn.telemetry or {}).get("backend") if turn else None,
                           unsupported_claim=bool(CLAIM_RE.search(reply)) and not tools,
                           reply=reply[:160])
            except Exception as e:
                row.update(answered=False, error=f"{type(e).__name__}: {e}"[:200])
            try:
                import httpx
                st = httpx.get(base + "/status", timeout=10).json()
                row.update(router_up=st.get("router_up"), router_restarts=st.get("router_restarts"),
                           router_restarts_hung=st.get("router_restarts_hung"),
                           healthy_models=len(st.get("router_models") or []))
            except Exception as e:
                row["status_error"] = f"{type(e).__name__}: {e}"[:120]
            p = backend_process()
            if p is None:
                row["server"] = "gone"
            else:
                try:
                    row.update(server="up", rss_mb=round(p.memory_info().rss / 2 ** 20, 1),
                               cpu_percent=p.cpu_percent(interval=0.5), threads=p.num_threads())
                except Exception:
                    row["server"] = "unreadable"
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            time.sleep(max(5.0, args.every))
        out.write(json.dumps({"kind": "end", "t": time.time()}) + "\n")
    print(summarise(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
