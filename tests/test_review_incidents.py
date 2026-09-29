"""The hourly review (scripts/review_incidents.py): which turns go to the
model, what it is shown, and what of its answer is kept. The model itself is
never called here."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import review_incidents as r  # noqa: E402


def _write(path, recs, tail=""):
    path.write_text("".join(json.dumps(x) + "\n" for x in recs) + tail, encoding="utf-8")


def test_only_the_owners_new_complete_turns_are_reviewed(tmp_path):
    p = tmp_path / "turns.jsonl"
    _write(p, [{"client": "desktop", "user": "old"}])
    _, end = r.new_turns(str(p), 0)
    _write(p, [{"client": "desktop", "user": "old"}, {"client": "session:smoke", "user": "smoke test"},
               {"client": "remote", "user": "from the phone"}], tail='{"client": "desktop", "user": "half')
    turns, end2 = r.new_turns(str(p), end)
    assert [t["user"] for t in turns] == ["from the phone"]
    assert end2 < p.stat().st_size                  # the half-written line waits for the next review


def test_a_log_that_started_over_is_read_from_the_top(tmp_path):
    p = tmp_path / "turns.jsonl"
    _write(p, [{"client": "desktop", "user": "new life"}])
    turns, _ = r.new_turns(str(p), 10_000)
    assert [t["user"] for t in turns] == ["new life"]


def test_the_model_is_told_which_turns_a_rule_already_caught():
    prompt = r.prompt_for([
        {"client": "desktop", "user": "play woman by harry styles", "backend": "agent",
         "reply": "Clicked \"Play Juice (Lizzo cover)\" in Brave.", "seconds": 4, "tools": ["app_step"]},
        {"client": "desktop", "user": "play it again", "backend": "no-model",
         "reply": "My model didn't answer in time, sir.", "seconds": 40, "tools": []},
    ])
    first, second = prompt.split("[2]")
    assert "FLAGGED" not in first and "tools: app_step" in first
    assert "FLAGGED: no_model" in second


def test_only_findings_about_a_listed_turn_are_kept(tmp_path):
    turns = [{"client": "desktop", "user": "play woman by harry styles", "reply": "Clicked Juice."}]
    kept = r.record([
        {"turn": 1, "kind": "wrong_action", "severity": "error", "why": "Played Juice, not Woman."},
        {"turn": 7, "kind": "other", "severity": "error", "why": "a turn that does not exist"},
        {"turn": "1", "kind": "other", "severity": "warning", "why": "not a number"},
    ], turns, path=str(tmp_path / "incidents.jsonl"))
    assert [k["kind"] for k in kept] == ["review_wrong_action"]
    rec = json.loads((tmp_path / "incidents.jsonl").read_text(encoding="utf-8"))
    assert rec["source"] == "review" and rec["turn"]["user"] == "play woman by harry styles"


def test_nothing_is_written_when_the_model_finds_nothing(tmp_path):
    assert r.record([], [{"user": "hi"}], path=str(tmp_path / "incidents.jsonl")) == []
    assert not (tmp_path / "incidents.jsonl").exists()
