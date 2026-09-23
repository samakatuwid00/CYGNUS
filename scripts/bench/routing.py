"""Routing accuracy, measured on what was actually said.

The routing target in docs/plans/OPEN_SOURCE_PLAN.md ("95%+ on routes that
act") needs labelled utterances, and invented sentences do not measure it: they
are written by someone who knows the phrase tables. So mine the real ones out
of data/sessions/, group the near-duplicates, and let the owner confirm a label
per group.

    python scripts/bench/routing.py mine     # sessions  -> data/routing_review.jsonl
    python scripts/bench/routing.py accept   # review    -> data/routing_gold.jsonl
    python scripts/bench/routing.py score    # gold set  -> accuracy, and the misses

`mine` proposes the label the deterministic classifier gives today, so leaving
a group alone means "the classifier is right here". That is honest as long as
the groups the classifier is *unsure* about are read first, which is why the
review file leads with the groups whose members disagree with each other, and
with the ones that fell through to "general".

Everything stays in data/ (gitignored): these are the owner's own words.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cygnus import paths  # noqa: E402
from cygnus.brain.intents import classify_intent  # noqa: E402

REVIEW = paths.DATA_DIR / "routing_review.jsonl"
GOLD = paths.DATA_DIR / "routing_gold.jsonl"

# Labels where a wrong route does something to the machine, or fails to. These
# are the ones the 95% target is about; a missed "thanks" costs nothing.
ACTING = {
    "launch", "open_site", "close_app", "music", "stop", "report", "add_site",
    "rescan", "running_stop", "running_front", "running_text",
    "running_describe", "running_explain", "app_install", "app_uninstall",
    "web_browse", "job_stop", "remember", "app_reference",
}

# Turns that are answers to Cygnus, not requests to it. They route by context,
# not by phrasing, so they do not belong in a gold set about phrasing.
_ANSWERS = {
    "yes", "no", "yeah", "yep", "nope", "sure", "ok", "okay", "cancel",
    "stop it", "go ahead", "do it", "sige", "hindi", "the first one",
    "the second one", "first", "second", "third", "both", "neither",
}


def _norm(text: str) -> str:
    """Lowercase, punctuation out, runs of space in - for comparing phrasings."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _signature(text: str) -> str:
    """A crude bucket key: the sorted words, so word order does not split a group."""
    return " ".join(sorted(set(_norm(text).split())))


def read_utterances() -> Counter:
    """Every user turn in data/sessions/, counted, answers and noise dropped."""
    said: Counter = Counter()
    for f in sorted(paths.SESSIONS_DIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("role") != "user":
                continue
            text = (row.get("text") or "").strip()
            n = _norm(text)
            # One word is almost always a wake word, a filler or a mis-hear;
            # 200+ characters is dictation, which routes as "general" anyway.
            if not n or n in _ANSWERS or len(n.split()) < 2 or len(text) > 200:
                continue
            said[text] += 1
    return said


def cluster(said: Counter, ratio: float = 0.7) -> list[dict]:
    """Group utterances that are the same request said differently.

    Bucket by word set first (cheap), then merge buckets whose representatives
    are close by difflib. Quadratic in the number of buckets, which is small.

    Two phrasings only merge if they route the same way today. Without that,
    "open chrome" and "close chrome" merge at any useful ratio - one word apart,
    opposite actions - and the group gets one label for two requests.
    """
    buckets: dict[str, list[str]] = {}
    for text in said:
        buckets.setdefault(_signature(text), []).append(text)

    groups: list[dict] = []
    for members in buckets.values():
        rep = max(members, key=lambda t: (said[t], -len(t)))
        route = classify_intent(rep)
        placed = False
        for g in groups:
            if g["route"] != route:
                continue
            if difflib.SequenceMatcher(None, _norm(rep), _norm(g["rep"])).ratio() >= ratio:
                g["members"].extend(members)
                placed = True
                break
        if not placed:
            groups.append({"rep": rep, "route": route, "members": list(members)})

    out = []
    for g in groups:
        members = sorted(set(g["members"]), key=lambda t: -said[t])
        g.pop("route")
        routes = Counter(classify_intent(t) for t in members)
        proposed, _ = routes.most_common(1)[0]
        out.append({
            "rep": g["rep"],
            "proposed": proposed,
            "label": None,          # the owner fills this in to disagree
            "count": sum(said[t] for t in members),
            "variants": len(members),
            "agree": len(routes) == 1,
            "routes": dict(routes),
            "examples": members[:4],
            "members": members,
        })
    out.sort(key=_review_order)
    return out


def _review_order(row: dict):
    """Cheapest doubt first, so a short review session buys the most.

    1. groups whose own phrasings route differently from each other - the
       classifier is unstable there, and one of the two answers is wrong.
    2. groups that act, most-said first - a wrong route here does something.
    3. everything else, most-said first. "general" means "ask the model",
       which is the safe fallback and the least urgent to confirm.
    """
    return (
        row["agree"],
        row["proposed"] not in ACTING,
        -row["count"],
    )


def cmd_mine(args) -> int:
    said = read_utterances()
    if not said:
        print(f"No user turns in {paths.SESSIONS_DIR}. Use Cygnus for a while first.")
        return 1
    groups = cluster(said, ratio=args.ratio)
    kept = groups[:args.limit] if args.limit else groups

    REVIEW.parent.mkdir(parents=True, exist_ok=True)
    with REVIEW.open("w", encoding="utf-8") as fh:
        for row in kept:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    turns = sum(said.values())
    split = Counter(r["proposed"] for r in kept)
    print(f"{turns} user turns, {len(said)} distinct, {len(groups)} groups")
    print(f"{len(kept)} groups to review -> {REVIEW}")
    print(f"  they cover {sum(r['count'] for r in kept)} of {turns} turns "
          f"({100 * sum(r['count'] for r in kept) / turns:.0f}%)")
    print(f"  {sum(1 for r in kept if not r['agree'])} route inconsistently "
          f"(first in the file, read these)")
    print(f"  {sum(1 for r in kept if r['proposed'] in ACTING)} propose a route that acts")
    print("  proposals: " + ", ".join(f"{k} {v}" for k, v in split.most_common(6)))
    if args.limit and len(groups) > args.limit:
        print(f"  {len(groups) - args.limit} quieter groups left out; "
              f"--limit 0 writes all of them")
    print("\nEdit \"label\" on any group the proposal gets wrong, then: "
          "python scripts/bench/routing.py accept")
    return 0


def cmd_accept(args) -> int:
    if not REVIEW.exists():
        print(f"No {REVIEW}. Run: python scripts/bench/routing.py mine")
        return 1

    gold: dict[str, dict] = {}
    if GOLD.exists():
        for line in GOLD.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                gold[row["text"]] = row

    added = changed = confirmed = skipped = 0
    for line in REVIEW.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # A group whose own phrasings route differently has no proposal worth
        # auto-accepting: one of its answers is already wrong. Those wait for a
        # person.
        if not row["agree"] and not row.get("label"):
            skipped += 1
            continue
        # A filled-in "label" is a person's judgement. Falling back to
        # "proposed" records the classifier's own answer, which cannot then be
        # used to measure the classifier - so which it was gets written down.
        label = row.get("label") or row["proposed"]
        source = "confirmed" if row.get("label") else "proposed"
        confirmed += source == "confirmed"
        for text in (row["members"] if args.all_variants else [row["rep"]]):
            was = gold.get(text)
            if was is None:
                added += 1
            elif was["label"] != label:
                changed += 1
            # Never downgrade a label a person set to an auto-accepted one.
            if was and was.get("source") == "confirmed" and source == "proposed":
                continue
            gold[text] = {"text": text, "label": label, "source": source}

    with GOLD.open("w", encoding="utf-8") as fh:
        for text in sorted(gold):
            fh.write(json.dumps(gold[text], ensure_ascii=False) + "\n")
    n_conf = sum(1 for r in gold.values() if r.get("source") == "confirmed")
    print(f"{len(gold)} labelled utterances in {GOLD} "
          f"({added} new, {changed} relabelled)")
    print(f"  {n_conf} carry a label a person set; {len(gold) - n_conf} took the "
          f"classifier's own proposal")
    if skipped:
        print(f"  {skipped} groups whose phrasings route inconsistently are still "
              f"waiting for a label (they are first in the review file)")
    if not confirmed:
        print("  nothing was edited, so `score` can only report what the "
              "classifier already says about itself")
    return 0


def cmd_score(args) -> int:
    if not GOLD.exists():
        print(f"No gold set at {GOLD}. Run mine, then accept.")
        return 1
    rows = [json.loads(l) for l in GOLD.read_text(encoding="utf-8").splitlines() if l.strip()]

    def tally(subset):
        hit = sum(1 for r in subset if classify_intent(r["text"]) == r["label"])
        return hit, len(subset)

    confirmed = [r for r in rows if r.get("source") == "confirmed"]
    acting = [r for r in rows if r["label"] in ACTING]
    acting_confirmed = [r for r in confirmed if r["label"] in ACTING]
    misses = [(r["text"], r["label"], classify_intent(r["text"]))
              for r in rows if classify_intent(r["text"]) != r["label"]]

    print(f"{len(rows)} labelled utterances, {len(confirmed)} of them confirmed by hand")
    for name, subset, target in (
        ("overall", rows, None),
        ("routes that act", acting, 95.0),
        ("confirmed only", confirmed, None),
        ("confirmed, acting", acting_confirmed, 95.0),
    ):
        if not subset:
            continue
        hit, total = tally(subset)
        pct = 100 * hit / total
        note = f"   target {target:.0f}%" if target else ""
        print(f"  {name:<18} {pct:5.1f}%  ({hit}/{total}){note}")
    if not confirmed:
        print("  Nothing is confirmed, so every row above is the classifier "
              "agreeing with itself. Edit data/routing_review.jsonl to measure.")
    if misses:
        print(f"\n{len(misses)} misses:")
        for text, want, got in misses[:args.show]:
            print(f"  {want:>16} -> {got:<16} {text[:70]}")
        if len(misses) > args.show:
            print(f"  ... {len(misses) - args.show} more")
    # A gold set that the classifier is measured against must not become a
    # target: failing the run would tempt patching the phrase table per row.
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mine", help="group real utterances and propose labels")
    m.add_argument("--ratio", type=float, default=0.7,
                   help="how close two phrasings must be to group (0-1, default 0.7)")
    m.add_argument("--limit", type=int, default=150,
                   help="how many groups to put up for review (0 = all)")
    m.set_defaults(func=cmd_mine)

    a = sub.add_parser("accept", help="fold the review file into the gold set")
    a.add_argument("--all-variants", action="store_true",
                   help="label every phrasing in a group, not just its representative")
    a.set_defaults(func=cmd_accept)

    s = sub.add_parser("score", help="measure classify_intent against the gold set")
    s.add_argument("--show", type=int, default=25, help="how many misses to print")
    s.set_defaults(func=cmd_score)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
