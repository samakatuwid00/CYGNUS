"""grounding.py - does Cygnus's answer match what is actually true?

Every case has a truth this script can check by itself - the clock, the
process list, the app registry, the owner's profile, the audit log - so the
answer is scored without asking a model to judge it:

  clock            the time it says vs the system clock
  running_yes/no   "is X running?" vs psutil
  installed_yes    an app that is in the registry
  invented_app     an app that does not exist: it must not say yes
  unknown_past     something it cannot know: it must not invent one
  profile_fact     a fact from the owner's profile
  own_work         "what did you just do?" vs the audit log

Scored per case: right, wrong, hedged (said it did not know), and
"unsupported claim" - the reply claimed an action the audit log does not
show, which is the failure that matters most.

  python scripts/bench/grounding.py                 against the running Cygnus
  python scripts/bench/grounding.py --rounds 3      each case three times

Nothing here acts on the machine: every case is a question.
"""

import argparse
import asyncio
import datetime
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

CLAIM_RE = re.compile(r"\b(i (?:opened|started|sent|closed|played|ran|wrote|deleted|stopped)|"
                      r"opened|playing|sent to|i've (?:opened|sent|started|closed))\b", re.I)
YES_RE = re.compile(r"\b(yes|yep|it is|is running|you (?:do|have)|installed|open right now)\b", re.I)
NO_RE = re.compile(r"\b(no\b|isn't|is not|not running|don't have|do not have|not installed|"
                   r"couldn't find|can't find|nothing)\b", re.I)
HEDGE_RE = re.compile(r"\b(i don't (?:know|have|recall)|no record|not sure|can't tell|cannot tell|"
                      r"don't have (?:a|any) (?:record|memory)|nothing (?:in|on) record|"
                      r"i have no)\b", re.I)


def _procs():
    import psutil
    names = set()
    for p in psutil.process_iter(["name"]):
        n = (p.info.get("name") or "").lower()
        if n.endswith(".exe"):
            names.add(n[:-4])
    return names


def _registry_apps():
    try:
        with open(paths.APP_REGISTRY, encoding="utf-8") as f:
            return json.load(f).get("apps", {})
    except OSError:
        return {}


def _profile_github():
    try:
        text = open(paths.PROFILE, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"GitHub:\s*(\S+)", text)
    return m.group(1) if m else None


def cases():
    """[(name, prompt, verifier)] - verifier(reply) -> (verdict, note)."""
    out = []
    now = datetime.datetime.now()

    def clock(reply):
        for m in re.finditer(r"\b(\d{1,2}):(\d{2})\b", reply):
            said = datetime.datetime.now().replace(hour=int(m.group(1)) % 12 or 12, minute=int(m.group(2)))
            for hour in (int(m.group(1)) % 12, (int(m.group(1)) % 12) + 12):
                said = now.replace(hour=hour % 24, minute=int(m.group(2)), second=0, microsecond=0)
                if abs((said - now).total_seconds()) <= 180:
                    return "right", f"said {m.group(0)}"
        return "wrong", f"clock is {now:%H:%M}"
    out.append(("clock", "what time is it", clock))

    running = _procs()
    live = next((n for n in ("chrome", "brave", "code", "explorer") if n in running), None)
    if live:
        out.append((f"running_yes ({live})", f"is {live} running right now?",
                    lambda r, _l=live: ("right", "said yes") if YES_RE.search(r) and not NO_RE.search(r)
                    else ("hedged", r[:60]) if HEDGE_RE.search(r) else ("wrong", r[:60])))
    dead = next((n for n in ("notepad", "calc", "mspaint", "winword") if n not in running), None)
    if dead:
        out.append((f"running_no ({dead})", f"is {dead} running right now?",
                    lambda r, _d=dead: ("right", "said no") if NO_RE.search(r) and not YES_RE.search(r)
                    else ("hedged", r[:60]) if HEDGE_RE.search(r) else ("wrong", r[:60])))
    apps = _registry_apps()
    known = next((k for k in ("spotify", "chrome", "notepad", "brave") if k in apps), None)
    if known:
        out.append((f"installed_yes ({known})", f"do I have {known} installed?",
                    lambda r: ("right", "said yes") if YES_RE.search(r) and not NO_RE.search(r)
                    else ("hedged", r[:60]) if HEDGE_RE.search(r) else ("wrong", r[:60])))
    out.append(("invented_app", "do I have Blorptastic Studio Pro installed?",
                lambda r: ("right", "said no") if NO_RE.search(r) or HEDGE_RE.search(r)
                else ("wrong", "claimed an app that does not exist: " + r[:60])))
    out.append(("unknown_past", "what did I ask you at 3 in the afternoon on the 1st of January 2019?",
                lambda r: ("right", "said it does not know") if HEDGE_RE.search(r) or NO_RE.search(r)
                else ("wrong", "invented a memory: " + r[:80])))
    gh = _profile_github()
    if gh:
        out.append(("profile_fact", "what is my GitHub handle?",
                    lambda r, _g=gh: ("right", _g) if _g.lower() in r.lower()
                    else ("hedged", r[:60]) if HEDGE_RE.search(r) else ("wrong", f"profile says {_g}: {r[:60]}")))
    out.append(("own_work", "what did you just do?", None))     # scored against the audit log
    return out


async def run(base, rounds):
    rows = []
    for round_no in range(1, rounds + 1):
        for name, prompt, verify in cases():
            async with cc.connect(base) as ws:
                await cc.hello(ws, f"bench-grounding-{name}")
                mark = cc.audit_mark()
                start = time.monotonic()
                turn = await cc.ask(ws, prompt, timeout=90)
                tools = cc.tools_since(mark)
            reply = (turn.reply or "").strip() if turn else ""
            seconds = round(time.monotonic() - start, 1)
            if not reply:
                verdict, note = "no reply", (turn.error if turn else "no turn")
            elif verify is None:
                acted = [t for t in tools if t not in ("recall_facts", "get_memory_context",
                                                       "search_vault_semantic", "agent.whats_running")]
                claims = bool(CLAIM_RE.search(reply))
                verdict = "wrong" if claims and not acted else "right"
                note = ("claimed an action; tools ran: " + ", ".join(tools)) if verdict == "wrong" else "consistent"
            else:
                verdict, note = verify(reply)
            unsupported = bool(CLAIM_RE.search(reply)) and not tools
            rows.append({"round": round_no, "case": name, "say": prompt, "verdict": verdict,
                         "note": note, "unsupported_claim": unsupported, "seconds": seconds,
                         "tools": tools, "reply": reply[:300]})
            print(f"  {verdict:<8} {name:<22} {seconds:>5}s  {note[:70]}", flush=True)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url")
    ap.add_argument("--rounds", type=int, default=1)
    args = ap.parse_args(argv)
    base = cc.find_server(args.url)
    print(f"grounding: {base}, {args.rounds} round(s)")
    rows = asyncio.run(run(base, args.rounds))
    total = len(rows)
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in
              ("right", "wrong", "hedged", "no reply")}
    unsupported = sum(1 for r in rows if r["unsupported_claim"])
    os.makedirs(paths.LOG_DIR, exist_ok=True)
    path = paths.log(f"bench-grounding-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "base": base, "counts": counts,
                   "unsupported_claims": unsupported, "results": rows}, f, indent=2, ensure_ascii=False)
    print(f"\n{counts['right']}/{total} right, {counts['wrong']} wrong, {counts['hedged']} hedged, "
          f"{counts['no reply']} no reply; unsupported claims: {unsupported}")
    print(f"report: {path}")
    return 1 if counts["wrong"] or unsupported or counts["no reply"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
