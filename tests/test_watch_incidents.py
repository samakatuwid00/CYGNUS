"""The incident watcher (scripts/watch_incidents.py): it reads only complete
new lines, starts over on a new log, writes each problem down with its turn
and the log lines before it, and announces a repeat at most every ten minutes."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import watch_incidents as w  # noqa: E402


def test_the_follower_reads_only_complete_new_lines(tmp_path):
    p = tmp_path / "log.txt"
    p.write_bytes(b"old line\n")
    f = w.Follower(str(p))                     # from now on: the old line is not news
    assert f.read() == []
    with open(p, "ab") as fh:
        fh.write(b"first\nsecond, half")
    assert f.read() == ["first"]
    with open(p, "ab") as fh:
        fh.write(b" done\r\n")
    assert f.read() == ["second, half done"]


def test_the_follower_starts_over_when_the_log_does(tmp_path):
    p = tmp_path / "log.txt"
    p.write_bytes(b"a long first life of the log\n")
    f = w.Follower(str(p), offset=0)
    assert f.read() == ["a long first life of the log"]
    p.write_bytes(b"new\n")                     # the backend restarted and the log is new
    assert f.read() == ["new"]


def test_a_missing_file_is_nothing_yet(tmp_path):
    assert w.Follower(str(tmp_path / "not-yet.log")).read() == []


def _watcher(tmp_path, clock):
    shown = []
    watcher = w.Watcher(notify=lambda t, b: shown.append((t, b)), now=lambda: clock[0],
                        incidents_path=str(tmp_path / "incidents.jsonl"))
    return watcher, shown


def _written(tmp_path):
    return [json.loads(x) for x in (tmp_path / "incidents.jsonl").read_text(encoding="utf-8").splitlines()]


def test_a_failed_turn_is_written_with_its_turn_and_what_came_before(tmp_path):
    clock = [1_790_000_000.0]
    watcher, shown = _watcher(tmp_path, clock)
    watcher.log_lines(['INFO:     127.0.0.1:1 - "GET /status HTTP/1.1" 200 OK',
                       "[JARVIS] 9router/mimo unavailable (503); falling back to local Ollama...",
                       "[JARVIS] Ollama unavailable (Request timed out.); no model left to try."])
    watcher.turn_lines([json.dumps({"user": "play woman by harry styles", "backend": "no-model",
                                    "reply": "My model didn't answer in time, sir.", "seconds": 41.0})])
    [rec] = _written(tmp_path)
    assert rec["kind"] == "no_model" and rec["turn"]["user"] == "play woman by harry styles"
    assert rec["context"][-1].startswith("[JARVIS] Ollama unavailable")
    assert not any("GET /status" in c for c in rec["context"])        # polling is not context
    assert len(shown) == 1 and "no model answered" in shown[0][0]


def test_a_repeat_is_counted_but_announced_at_most_every_ten_minutes(tmp_path):
    clock = [1_790_000_000.0]
    watcher, shown = _watcher(tmp_path, clock)
    turn = json.dumps({"user": "hi", "backend": "no-model", "reply": "No model answered, sir.", "seconds": 2})
    watcher.turn_lines([turn, turn])
    clock[0] += w.TOAST_AGAIN_S + 1
    watcher.turn_lines([turn])
    assert [r["repeat"] for r in _written(tmp_path)] == [1, 2, 3]
    assert len(shown) == 2


def test_a_traceback_in_the_log_is_one_incident(tmp_path):
    watcher, shown = _watcher(tmp_path, [1_790_000_000.0])
    watcher.log_lines(["Traceback (most recent call last):", '  File "x.py", line 1, in f',
                       "KeyError: 'tool_call_id'", "[JARVIS Web] Ready."])
    [rec] = _written(tmp_path)
    assert rec["kind"] == "traceback" and rec["detail"] == "KeyError: 'tool_call_id'"


def test_a_good_turn_writes_nothing(tmp_path):
    watcher, shown = _watcher(tmp_path, [1_790_000_000.0])
    watcher.turn_lines([json.dumps({"user": "hi", "backend": "agent", "reply": "Hello, sir.", "seconds": 4})])
    assert not (tmp_path / "incidents.jsonl").exists() and shown == []


def test_a_warning_is_written_down_but_not_announced(tmp_path):
    watcher, shown = _watcher(tmp_path, [1_790_000_000.0])
    watcher.log_lines(['[WS] error: Cannot call "send" once a close message has been sent.'])
    watcher.turn_lines([json.dumps({"user": "hi", "backend": "agent", "reply": "Hello.", "seconds": 90})])
    assert [r["kind"] for r in _written(tmp_path)] == ["websocket_error", "slow_turn"]
    assert shown == []
