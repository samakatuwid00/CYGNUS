"""latency.py - how long each kind of turn takes, by the route that answered.

"Cygnus feels slow" is not measurable; this is. Each case is asked --rounds
times and the timings are grouped by the route the server reports (instant,
rule, hermes, agent, router, ollama...), so a slow cloud model does not hide
behind a fast local answer:

  instant   time, maths, greeting, a stop with nothing playing
  recall    what it remembers, the sites it knows, job status
  agent     questions that need the model and its tools

Targets (docs/plans/OPEN_SOURCE_PLAN.md): instant p95 under 300 ms, a turn
that needs the model p95 under 20 s.

  python scripts/bench/latency.py --rounds 5
  python scripts/bench/latency.py --only instant

Read-only: no case opens, closes or plays anything.
"""

import argparse
import asyncio
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from cygnus import paths  # noqa: E402
import cygnus_client as cc  # noqa: E402

TARGETS = {"instant": 0.3, "rule": 2.0, "hermes": 25.0, "agent": 20.0, "router": 20.0, "ollama": 60.0}
GROUPS = {
    "instant": ["what time is it", "17 * 23", "hello cygnus", "thanks", "stop the music",
                "anong oras na ngayon?"],
    "recall": ["what do you remember about me?", "what sites do you know?",
               "what's the status of the task?", "what apps do I have?"],
    "agent": ["can you visit websites?", "in one sentence, what is TCP?",
              "what coding agent did you use for the hello world website?"],
}


async def run(base, groups, rounds):
    rows = []
    for round_no in range(1, rounds + 1):
        for group, says in groups.items():
            for say in says:
                async with cc.connect(base) as ws:
                    await cc.hello(ws, "bench-latency")
                    start = time.monotonic()
                    turn = await cc.ask(ws, say, timeout=120)
                seconds = round(time.monotonic() - start, 3)
                tel = turn.telemetry if turn else {}
                # think: what the server spent on the turn. wait: what the user
                # waits, which here includes a fresh socket for every case.
                think = round((turn.ms or 0) / 1000, 3) if turn else 0.0
                rows.append({"round": round_no, "group": group, "say": say, "seconds": seconds,
                             "think": think, "backend": tel.get("backend"), "intent": tel.get("intent"),
                             "model": tel.get("model"), "answered": bool(turn and turn.reply)})
                print(f"  {group:<8} think {think:>7.3f}s  wait {seconds:>7.3f}s  "
                      f"{str(tel.get('backend')):<10} {str(tel.get('model') or ''):<22} {say[:36]}", flush=True)
    return rows


def summarise(rows):
    by = {}
    for r in rows:
        if not r["answered"]:
            continue
        key = r["backend"] or "unknown"
        by.setdefault(key, []).append(r)
    out = {}
    for key, rs in sorted(by.items()):
        think = [r["think"] for r in rs]
        wait = [r["seconds"] for r in rs]
        out[key] = {"turns": len(rs),
                    "think_p50": cc.percentile(think, 0.5), "think_p95": cc.percentile(think, 0.95),
                    "wait_p50": cc.percentile(wait, 0.5), "wait_p95": cc.percentile(wait, 0.95),
                    "max_wait": max(wait), "target_p95": TARGETS.get(key)}
    return out


def verdicts(summary, rows):
    found = []
    for key, s in summary.items():
        target = s["target_p95"]
        if target and s["think_p95"] > target:
            found.append(f"{key}: the server spent p95 {s['think_p95']:.2f}s, over the "
                         f"{target:.2f}s target ({s['turns']} turns)")
    missing = [r["say"] for r in rows if not r["answered"]]
    if missing:
        found.append(f"{len(missing)} turn(s) never answered: {missing[:3]}")
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--only", choices=sorted(GROUPS), help="one group only")
    args = ap.parse_args(argv)
    base = cc.find_server(args.url)
    groups = {args.only: GROUPS[args.only]} if args.only else GROUPS
    print(f"latency: {base}, {args.rounds} round(s) of {sum(len(v) for v in groups.values())} turns")
    rows = asyncio.run(run(base, groups, args.rounds))
    summary = summarise(rows)
    found = verdicts(summary, rows)
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    path = paths.log(f"bench-latency-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "base": base, "summary": summary,
                   "findings": found, "results": rows}, f, indent=2, ensure_ascii=False)
    print("\n  route        turns  think p50  think p95   wait p95   target")
    for key, s in summary.items():
        t = f"{s['target_p95']:.2f}s" if s["target_p95"] else "-"
        print(f"  {key:<12} {s['turns']:>5}  {s['think_p50']:>8.3f}s  {s['think_p95']:>8.3f}s  "
              f"{s['wait_p95']:>8.3f}s  {t:>7}")
    print("\n" + ("\n".join("  - " + f for f in found) if found else "  every route within target"))
    print(f"report: {path}")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
