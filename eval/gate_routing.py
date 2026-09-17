"""gate_routing.py - the pass/fail oracle for the routing fixes.

Exit 0 only when every check below holds. Written before the fixes, and RED at
commit 994e70f: clarify, multi and malformed are all at zero there.

Each check is deterministic - keyword_route is regexes and registry lookups,
no model call, no network - so a run means the same thing twice. That is the
whole point: the agent_loop numbers swing 30 points between identical runs and
cannot gate anything.

Two checks exist to stop a fix that cheats:

  NO_REGRESSION  the gold set must not get worse while the stress set gets
                 better. Special-casing the stress phrasings would show up here.
  LATENCY        keyword_route must stay under 200ms at p99. Routing a hard
                 case to a model would satisfy every accuracy check and make
                 the router 20,000x slower.

  python eval/gate_routing.py          run the gate
  python eval/gate_routing.py -v       also print the numbers behind each check
"""

from __future__ import annotations

import argparse
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import numpy as np                      # noqa: E402

import routing_eval as rev              # noqa: E402
import stress_eval as sev               # noqa: E402

# Baselines measured at 994e70f. A check that is already passing is pinned at
# its measured value so it cannot silently rot while something else is fixed.
GOLD_FLOOR = 0.541          # keyword replay on eval/routing_gold.jsonl
STRESS_FLOOR = 0.215        # keyword replay on eval/stress_prompts.jsonl
P99_CEILING_MS = 200.0      # measured 71ms; a model call would be seconds


class Check:
    def __init__(self, name, ok, got, want):
        self.name, self.ok, self.got, self.want = name, ok, got, want

    def line(self):
        return f"  {'PASS' if self.ok else 'FAIL'}  {self.name:<38} got {self.got}, want {self.want}"


def gold_scores():
    gold = rev.load_gold()
    s = rev.score_keyword(gold)
    return s, gold


def recall(scored, route):
    n, hits = scored["per_route"].get(route, (0, 0))
    return (hits / n if n else 0.0), n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    checks: list[Check] = []

    # --- gold set -----------------------------------------------------------
    gold_s, _ = gold_scores()
    checks.append(Check("gold accuracy (no regression)",
                        gold_s["accuracy"] >= GOLD_FLOOR,
                        f"{gold_s['accuracy']:.1%}", f">= {GOLD_FLOOR:.1%}"))

    for route in ("clarify", "multi"):
        r, n = recall(gold_s, route)
        checks.append(Check(f"gold recall: {route} ({n} rows)",
                            r > 0.0, f"{r:.0%}", "> 0%"))

    # --- stress set ---------------------------------------------------------
    rows, lines = sev.load_prompts(sev.PROMPTS)
    checks.append(Check("stress file parses fully",
                        len(rows) == lines, f"{len(rows)}/{lines} lines", "all"))

    results = sev.measure(rows)
    pairs = sev.scorable(results)
    stress_s = rev.score(pairs)
    checks.append(Check("stress accuracy (no regression)",
                        stress_s["accuracy"] >= STRESS_FLOOR,
                        f"{stress_s['accuracy']:.1%}", f">= {STRESS_FLOOR:.1%}"))

    groups = sev.by_category(results)
    for cat, floor in (("malformed", 1.0), ("anaphora", 0.5), ("multi_intent", 0.5)):
        rs = groups.get(cat, [])
        pr = [(r["row"], r["route"]) for r in rs if r["exc_type"] is None]
        acc = rev.score(pr)["accuracy"] if pr else 0.0
        checks.append(Check(f"stress category: {cat} ({len(rs)} rows)",
                            acc >= floor, f"{acc:.0%}", f">= {floor:.0%}"))

    # --- safety -------------------------------------------------------------
    safety = sev.safety(results)
    checks.append(Check("safety: unguarded acting routes",
                        len(safety["violations"]) == 0,
                        str(len(safety["violations"])), "0"))

    # --- robustness ---------------------------------------------------------
    errs = sev.robustness(results)
    checks.append(Check("robustness: utterances that raised",
                        len(errs) == 0, str(len(errs)), "0"))

    # --- latency (anti-cheat) ----------------------------------------------
    lat = sev.latency(results)
    p99 = lat.get("p99", 0.0)
    checks.append(Check("keyword_route p99 latency",
                        p99 < P99_CEILING_MS, f"{p99:.1f}ms", f"< {P99_CEILING_MS:.0f}ms"))

    # --- report -------------------------------------------------------------
    failed = [c for c in checks if not c.ok]
    print(f"routing gate: {len(checks) - len(failed)}/{len(checks)} checks pass")
    for c in checks:
        print(c.line())
    if args.verbose:
        print("\n  gold per-route recall:")
        for route, (n, h) in sorted(gold_s["per_route"].items()):
            print(f"    {route:<16} {h:>3}/{n:<4} {h / n:>4.0%}")
    if failed:
        print(f"\nGATE FAILED: {len(failed)} check(s)")
        for c in failed:
            print(f"  - {c.name}: got {c.got}, want {c.want}")
        return 1
    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    print(f"({time.time() - t0:.0f}s)")
    sys.exit(rc)
