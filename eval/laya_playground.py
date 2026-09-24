"""laya_playground.py - try Laya on CYGNUS utterances in a browser.

Type an utterance, see the route Laya picks, its probability for every
route, and how long the call took. Sample buttons draw from the gold and
stress sets and show the label next to Laya's pick. A custom mode takes any
question and options, for trying Laya on something other than routing.

Serves on 127.0.0.1 only: the page runs model calls on request and has no
authentication, so it must not listen on the network.

  .venv.laya/Scripts/python eval/laya_playground.py            http://127.0.0.1:8765
  .venv.laya/Scripts/python eval/laya_playground.py --port 9000 --no-open
"""

import argparse
import json
import os
import random
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from eval import laya_eval as le  # noqa: E402

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "laya_playground.html")


class Model:
    """One Laya agent; torch calls are serialised so requests cannot interleave."""

    def __init__(self):
        import laya
        t0 = time.perf_counter()
        self.agent = laya.load(le.REPO)
        self.agent.cfg["head_max_len"] = 448        # see laya_eval.main
        self.load_s = time.perf_counter() - t0
        self.schemas = le.Schemas(self.agent)
        self.lock = Lock()
        self.predict({"utterance": "warm up"}, self.schemas.labels)

    def predict(self, state, questions):
        with self.lock:
            t0 = time.perf_counter()
            res = self.agent.predict(state, questions)
            return res, (time.perf_counter() - t0) * 1000.0

    def route(self, text, schema):
        s = self.schemas
        state = {"utterance": text}
        if schema in ("labels", "described"):
            res, ms = self.predict(state, s.labels if schema == "labels" else s.described)
            return {"steps": [{"name": schema, "answer": res["answers"]["q"]}], "ms": ms,
                    "route": res["answers"]["q"]["choice"]}
        fam, ms1 = self.predict(state, s.family)
        fa = fam["answers"]["q"]
        steps = [{"name": "family", "answer": fa}]
        if fa["choice"] not in s.within:
            return {"steps": steps, "ms": ms1, "route": le.FAMILIES[fa["choice"]][1][0]}
        res, ms2 = self.predict(state, s.within[fa["choice"]])
        steps.append({"name": f"within {fa['choice']}", "answer": res["answers"]["q"]})
        return {"steps": steps, "ms": ms1 + ms2, "route": res["answers"]["q"]["choice"]}

    def custom(self, text, qtype, instructions, options):
        q = {"type": qtype, "instructions": instructions}
        if qtype == "choice":
            q["criteria"] = options
        elif qtype == "score":
            q["criteria"] = list(options)
        res, ms = self.predict({"text": text}, {"q": q})
        return {"steps": [{"name": qtype, "answer": res["answers"]["q"]}], "ms": ms}


def samples():
    rows = []
    for name, path in (("gold", le.GOLD), ("stress", le.STRESS)):
        for r in le.load(path):
            rows.append({"text": r["text"], "gold": r["route"], "set": name,
                         "category": r.get("category") or ""})
    return rows


def make_handler(model, rows):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                with open(PAGE, "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if self.path == "/meta":
                return self.send(200, {"routes": le.ROUTES,
                                       "families": {k: {"description": d, "routes": r}
                                                    for k, (d, r) in le.FAMILIES.items()},
                                       "load_s": round(model.load_s, 1)})
            if self.path.startswith("/sample"):
                pool = [r for r in rows if "stress" not in self.path or r["set"] == "stress"]
                return self.send(200, random.sample(pool, min(8, len(pool))))
            self.send(404, {"error": "not found"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                text = str(req.get("text") or "").strip()
                if not text:
                    return self.send(400, {"error": "empty utterance"})
                if self.path == "/route":
                    return self.send(200, model.route(text, req.get("schema") or "described"))
                if self.path == "/custom":
                    return self.send(200, model.custom(text, req.get("type") or "choice",
                                                       req.get("instructions") or "",
                                                       req.get("options") or {}))
                self.send(404, {"error": "not found"})
            except Exception as e:      # shown on the page rather than killing the server
                self.send(500, {"error": f"{type(e).__name__}: {e}"})
    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)
    print("loading laya...", flush=True)
    model = Model()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(model, samples()))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"laya loaded in {model.load_s:.1f}s - serving {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
