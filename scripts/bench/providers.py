"""providers.py - which model a student should use, measured.

Sends Cygnus's real first agent call - the agent loop's system prompt and all
its tools - straight to each provider and model, for requests whose right
first move is known, and scores what comes back:

  right     the tool it called (or plain words, for a question) is the right one
  seconds   time to the reply; p50 and the slowest
  prompt    prompt tokens the provider counted: what one call costs
  errors    refusals, quota walls, timeouts - quoted, not hidden

One call, not a whole turn: a turn makes 2-4 of these, so a turn takes
roughly that many times as long and spends that many requests of a daily
quota. Read-only: tools are chosen, never run.

Keys come from the environment (or .env): GOOGLE_AI_API_KEY, GROQ_API_KEY,
OPENROUTER_API_KEY, OPENAI_API_KEY. Ollama needs none.

  python scripts/bench/providers.py --gap 13 gemini:gemini-2.5-flash gemini:gemini-3.5-flash
  python scripts/bench/providers.py ollama:qwen3:4b-instruct ollama:qwen3:1.7b
  python scripts/bench/providers.py groq:llama-3.3-70b-versatile --rounds 2
  python scripts/bench/providers.py --list-cases
"""

import argparse
import json
import os
import re
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from cygnus import config, paths  # noqa: E402
from cygnus.brain import agent_loop  # noqa: E402

ENDPOINTS = {
    "gemini": (config.GOOGLE_AI_BASE_URL, "GOOGLE_AI_API_KEY"),
    "groq": (config.PROVIDERS["groq"]["url"], "GROQ_API_KEY"),
    "openrouter": (config.PROVIDERS["openrouter"]["url"], "OPENROUTER_API_KEY"),
    "openai": (config.PROVIDERS["openai"]["url"], "OPENAI_API_KEY"),
    "ollama": (config.OLLAMA_BASE_URL, None),
}

# (what the student says, the right first tool - None for a plain answer,
#  a word the tool's arguments must carry)
CASES = [
    ("open notepad", {"open_app"}, "notepad"),
    ("open youtube", {"open_site", "open_app"}, "youtube"),
    ("what's running on my computer right now?", {"whats_running"}, ""),
    ("search the web for the weather in Cebu tomorrow", {"search_web"}, "cebu"),
    ("what do you remember about me?", {"recall"}, ""),
    ("remember that my exam is on Friday", {"remember"}, "friday"),
    (r"what is in the file C:\notes\todo.txt?", {"read_file"}, "todo"),
    ("in one sentence, what is TCP?", None, ""),
]


def _client(provider: str, timeout: float):
    from openai import OpenAI
    base, key_name = ENDPOINTS[provider]
    key = (os.getenv(key_name) or "").strip() if key_name else "ollama"
    if not key:
        raise SystemExit(f"{provider}: no key - set {key_name} in the environment or .env")
    return OpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=0)


def _judge(message, want, word) -> bool:
    calls = message.tool_calls or []
    if want is None:
        return not calls and bool((message.content or "").strip())
    if not calls:
        return False
    first = calls[0].function
    return first.name in want and word in (first.arguments or "").lower()


def run_one(client, model: str, say: str, want, word) -> dict:
    messages = [{"role": "system", "content": agent_loop._system({})},
                {"role": "user", "content": say}]
    start = time.monotonic()
    try:
        r = client.chat.completions.create(model=model, messages=messages,
                                           tools=agent_loop.TOOLS, tool_choice="auto")
    except Exception as e:
        # Which limit it was, when the provider names it (Gemini's quotaId):
        # a per-minute wall means slow down, a per-day one means come back tomorrow.
        limit = re.search(r"quotaId': '([^']+)", str(e))
        return {"say": say, "ok": False, "seconds": round(time.monotonic() - start, 2),
                "error": f"{type(e).__name__}: {str(e)[:160]}", "limit": limit.group(1) if limit else None}
    msg = r.choices[0].message
    calls = msg.tool_calls or []
    return {"say": say, "ok": _judge(msg, want, word), "seconds": round(time.monotonic() - start, 2),
            "called": calls[0].function.name if calls else None,
            "prompt_tokens": getattr(r.usage, "prompt_tokens", None) if r.usage else None}


def summarize(target: str, rows: list) -> dict:
    answered = [r for r in rows if "error" not in r]
    times = sorted(r["seconds"] for r in answered)
    prompts = [r["prompt_tokens"] for r in answered if r.get("prompt_tokens")]
    return {"target": target, "cases": len(rows), "right": sum(r["ok"] for r in rows),
            "errors": len(rows) - len(answered),
            "p50_s": round(statistics.median(times), 2) if times else None,
            "max_s": times[-1] if times else None,
            "prompt_tokens": round(statistics.mean(prompts)) if prompts else None,
            "first_error": next((r["error"] for r in rows if "error" in r), "")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("targets", nargs="*", help="provider:model, e.g. gemini:gemini-2.5-flash")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds per call (Ollama is slow)")
    ap.add_argument("--gap", type=float, default=0.0,
                    help="seconds between calls; 13 keeps under Gemini's free 5 a minute per model")
    ap.add_argument("--list-cases", action="store_true")
    args = ap.parse_args(argv)
    if args.list_cases or not args.targets:
        for say, want, _ in CASES:
            print(f"{say!r:55} -> {', '.join(sorted(want)) if want else 'a plain answer'}")
        return 0
    report = []
    for target in args.targets:
        provider, _, model = target.partition(":")
        if provider not in ENDPOINTS or not model:
            raise SystemExit(f"{target}: want provider:model, provider one of {', '.join(ENDPOINTS)}")
        client = _client(provider, args.timeout)
        rows = []
        for n in range(1, args.rounds + 1):
            for case in CASES:
                if rows and args.gap:
                    time.sleep(args.gap)
                rows.append(dict(run_one(client, model, *case), round=n))
        for r in rows:
            mark = "ok " if r["ok"] else "BAD"
            print(f"  {mark} {target:40} {r['seconds']:6.2f}s  {r.get('called') or r.get('error', 'answered')!s:40.40}  {r['say']}")
        report.append({**summarize(target, rows), "rows": rows})
    print(f"\n{'target':42} {'right':>7} {'err':>4} {'p50 s':>7} {'max s':>7} {'prompt':>7}")
    for s in report:
        print(f"{s['target']:42} {s['right']:>3}/{s['cases']:<3} {s['errors']:>4} "
              f"{s['p50_s'] if s['p50_s'] is not None else '-':>7} {s['max_s'] if s['max_s'] is not None else '-':>7} "
              f"{s['prompt_tokens'] or '-':>7}")
    out = paths.log(f"bench-providers-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"when": time.strftime("%Y-%m-%d %H:%M"), "rounds": args.rounds, "report": report}, f, indent=2)
    print(f"\n{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
