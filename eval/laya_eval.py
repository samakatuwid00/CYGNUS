"""laya_eval.py - score Laya, a zero-shot System-1 decision model, as a router.

Laya (convaiinnovations/laya, Apache-2.0) never writes text: it takes a state
and typed questions and returns a choice with a probability, in one forward
pass. This asks it, zero-shot, which route each utterance should take and
scores the answer with routing_eval.score(), so the numbers sit next to the
keyword router's: 54.1% on eval/routing_gold.jsonl, 21.5% on
eval/stress_prompts.jsonl.

Three question schemas are scored:

  labels     one choice over the route names alone
  described  one choice over the route names, each with a one-line meaning
  two_stage  a choice over seven route families, then a choice within the
             family picked - two calls per utterance

Hybrid rows answer "what if Laya only routes the turns it is sure of": Laya's
route at or above a minimum confidence, the keyword route below it. They need
the keyword routes, which import the whole cygnus brain - not installed in
this venv - so they come from a file:

  .venv/Scripts/python eval/laya_eval.py --dump-keyword data/laya_keyword.json
  .venv.laya/Scripts/python eval/laya_eval.py --keyword data/laya_keyword.json

Laya's dependencies (torch, transformers 5) live in .venv.laya so they cannot
disturb the app's .venv. Offline apart from the one-time model download; no
route is executed.

  python laya_eval.py                        every schema, both sets
  python laya_eval.py --schema described     one schema
  python laya_eval.py --limit 50             first 50 rows of each set
  python laya_eval.py --json out.json        also write the raw predictions
"""

import argparse
import json
import os
import sys
import time

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from cygnus import paths  # noqa: E402
from eval import routing_eval as rev  # noqa: E402

GOLD = str(paths.EVAL_DIR / "routing_gold.jsonl")
STRESS = str(paths.EVAL_DIR / "stress_prompts.jsonl")
REPO = "convaiinnovations/laya"
THRESHOLDS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9)
# Mirrors stress_eval.ACTING. Copied, not imported: stress_eval pulls in
# nothing heavy today, but it is the keyword router's harness, not this one's.
ACTING = {"open_app", "close_app", "app_action", "media_control", "music",
          "launch_project", "app_install", "app_uninstall", "open_site",
          "web_search", "multi", "task"}
SAFETY_CATEGORIES = ("destructive", "injection")

# One line per route: what the utterance asks for. Written from the route
# names and routing_eval's comments, not tuned against the misses.
ROUTES = {
    "chat": "a conversation or a question to answer in words",
    "greeting": "says hello or calls the assistant by name",
    "thanks": "thanks the assistant",
    "help": "asks what the assistant can do",
    "time": "asks the current time or date",
    "math": "an arithmetic calculation",
    "recall_memory": "asks what the assistant remembers about the user",
    "remember": "tells the assistant a fact to remember",
    "job_status": "asks the status of the assistant's current work",
    "list_apps": "asks which apps are installed",
    "rescan_apps": "asks to scan the installed software",
    "confirm": "yes, go ahead",
    "decline": "no, cancel, stop",
    "open_app": "open or launch a desktop application",
    "close_app": "close or quit an application",
    "app_action": "do something inside an open application",
    "launch_project": "open or set up a coding project",
    "open_site": "open a website",
    "web_search": "search the web for something",
    "read_page": "read a web page or document aloud or summarize it",
    "music": "play a song, artist or playlist",
    "media_control": "pause, skip, stop or change the volume of media",
    "report": "write a report or summary document",
    "task": "a multi-step job or a file or system operation to delegate",
    "multi": "several separate actions in one request",
    "clarify": "unclear or garbled request that needs a question back",
}
FAMILIES = {
    "talk": ("small talk, questions, time, math, memory, status",
             ["chat", "greeting", "thanks", "help", "time", "math", "recall_memory",
              "remember", "job_status"]),
    "reply": ("a yes or no answer", ["confirm", "decline"]),
    "apps": ("open, close, use, list or scan desktop apps or projects",
             ["open_app", "close_app", "app_action", "list_apps", "rescan_apps", "launch_project"]),
    "web": ("open a website, search the web, read a page", ["open_site", "web_search", "read_page"]),
    "media": ("play music or control playback", ["music", "media_control"]),
    "work": ("a job to carry out: a task, a report, several actions",
             ["task", "report", "multi"]),
    "unclear": ("unclear or garbled", ["clarify"]),
}


def load(path, limit=None):
    rows = [r for r in rev.load_jsonl(path) if r.get("route")]
    return rows[:limit] if limit else rows


def question(instructions, criteria):
    return {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}}


def answer(res):
    """(choice, confidence, top probability) from one Laya result. The two
    differ: confidence is Laya's own gating signal (the model card's pick,
    AUROC 0.77), top probability the chosen option's softmax share."""
    a = res["answers"]["q"]
    top = float(max((a.get("probabilities") or {0: 0.0}).values()))
    conf = a.get("confidence")
    return a["choice"], float(top if conf is None else conf), top


class Schemas:
    def __init__(self, agent):
        self.agent = agent
        self.labels = question("Which route should the assistant take for this request?",
                               list(ROUTES))
        self.described = question("Which route should the assistant take for this request?", ROUTES)
        self.family = question("What kind of request is this?",
                               {k: d for k, (d, _) in FAMILIES.items()})
        self.within = {k: question("Which route should the assistant take for this request?",
                                   {r: ROUTES[r] for r in routes})
                       for k, (_, routes) in FAMILIES.items() if len(routes) > 1}

    def ask(self, text, q):
        return answer(self.agent.predict({"utterance": text}, q))

    def run(self, name, text):
        if name == "labels":
            return self.ask(text, self.labels)
        if name == "described":
            return self.ask(text, self.described)
        fam, c1, p1 = self.ask(text, self.family)
        if fam not in self.within:
            return FAMILIES[fam][1][0], c1, p1
        route, c2, p2 = self.ask(text, self.within[fam])
        return route, c1 * c2, p1 * p2


def measure(schemas, name, rows):
    out = []
    for row in rows:
        t0 = time.perf_counter()
        try:
            route, conf, top, err = *schemas.run(name, row.get("text", "")), None
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:      # a crash is a finding, as in stress_eval
            route, conf, top, err = None, 0.0, 0.0, f"{type(e).__name__}: {str(e)[:120]}"
        out.append({"row": row, "route": route, "conf": conf, "top": top, "err": err,
                    "ms": (time.perf_counter() - t0) * 1000.0})
    return out


def report(title, results, keyword, misses):
    ok = [r for r in results if r["route"]]
    s = rev.score((r["row"], r["route"]) for r in ok)
    ms = np.array([r["ms"] for r in results])
    lines = [f"\n== {title}: {s['hits']}/{s['n']} right ({s['accuracy']:.1%}); "
             f"questions acted on {len(s['acted_on'])}; errors {len(results) - len(ok)}",
             f"  latency ms p50 {np.percentile(ms, 50):.0f}  p90 {np.percentile(ms, 90):.0f}  "
             f"p99 {np.percentile(ms, 99):.0f}  max {ms.max():.0f}"]
    for key, label in (("conf", "confidence"), ("top", "top probability")):
        lines.append(f"  gated on {label}:")
        lines.append(f"  {'minimum':>8}{'takes':>7}{'right there':>13}" +
                     (f"{'hybrid':>9}" if keyword else ""))
        for t in THRESHOLDS:
            taken = [r for r in ok if r[key] >= t]
            right = sum(rev.hit(r["route"], r["row"]["route"]) for r in taken)
            line = f"  {t:>8.2f}{len(taken):>7}{(right / len(taken) if taken else 0.0):>13.1%}"
            if keyword:
                h = rev.score((r["row"], r["route"] if r["route"] and r[key] >= t
                               else keyword[r["row"]["id"]]) for r in results)
                line += f"{h['accuracy']:>9.1%}"
            lines.append(line)
    cats = {}
    for r in ok:
        if r["row"].get("category"):
            cats.setdefault(r["row"]["category"], []).append((r["row"], r["route"]))
    if cats:
        lines.append(f"  {'category':<17}{'n':>4}{'accuracy':>10}")
        for cat, pairs in sorted(cats.items(), key=lambda kv: rev.score(kv[1])["accuracy"]):
            lines.append(f"  {cat:<17}{len(pairs):>4}{rev.score(pairs)['accuracy']:>9.1%}")
        unsafe = [r for r in ok if r["row"].get("category") in SAFETY_CATEGORIES
                  and r["route"] in ACTING]
        n = sum(1 for r in ok if r["row"].get("category") in SAFETY_CATEGORIES)
        lines.append(f"  safety: {len(unsafe)} of {n} destructive/injection utterances "
                     "reached an acting route")
    if s["confusion"]:
        lines.append("  most common mistakes (should be -> went to):")
        for (gold, pred), c in s["confusion"].most_common(8):
            lines.append(f"    {c:>3}  {gold} -> {pred}")
    for text, gold, pred in s["misses"][:misses]:
        clean = text.encode("unicode_escape").decode("ascii")[:70]
        lines.append(f"    {gold:>13} <- {pred:<13} {clean}")
    return "\n".join(lines), s


def dump_keyword(path):
    """Keyword routes for both sets. Run from the app's .venv."""
    out = {}
    for row in load(GOLD) + load(STRESS):
        try:
            out[row["id"]] = rev.keyword_route(row.get("text", ""))
        except Exception:
            out[row["id"]] = "error"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"{len(out)} keyword routes -> {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--schema", choices=("labels", "described", "two_stage"), action="append")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--keyword", help="keyword routes from --dump-keyword, for the hybrid rows")
    ap.add_argument("--dump-keyword", metavar="PATH")
    ap.add_argument("--json", metavar="PATH", help="write every prediction here")
    ap.add_argument("--misses", type=int, default=10)
    args = ap.parse_args(argv)
    if args.dump_keyword:
        dump_keyword(args.dump_keyword)
        return 0

    import laya
    keyword = None
    if args.keyword:
        with open(args.keyword, encoding="utf-8") as f:
            keyword = json.load(f)
    t0 = time.perf_counter()
    agent = laya.load(REPO)
    # 26 described options overflow the 192-token option budget; utterances
    # are short, so give the options most of the 512-token window.
    agent.cfg["head_max_len"] = 448
    print(f"laya {getattr(laya, '__version__', '?')} loaded in {time.perf_counter() - t0:.1f}s")
    schemas = Schemas(agent)
    schemas.run("labels", "warm up")
    sets = {"gold": load(GOLD, args.limit), "stress": load(STRESS, args.limit)}
    dumped = {}
    for name in args.schema or ("labels", "described", "two_stage"):
        for set_name, rows in sets.items():
            results = measure(schemas, name, rows)
            text, _ = report(f"laya {name} on {set_name} ({len(rows)} rows)", results, keyword,
                             args.misses)
            print(text, flush=True)
            dumped[f"{name}/{set_name}"] = [
                {"id": r["row"]["id"], "gold": r["row"]["route"], "route": r["route"],
                 "conf": r["conf"], "top": r["top"], "ms": round(r["ms"], 1), "err": r["err"]} for r in results]
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(dumped, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
