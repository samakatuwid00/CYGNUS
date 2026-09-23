""""No model answered" says which way it failed, and never pretends.

The old fallback was a canned "demo mode" that answered "Opening Notepad for
you, sir." while nothing opened, and every other failure read the same
(bench/chaos.py, 2026-09-23)."""

import pytest

from cygnus.brain import core as bg


@pytest.mark.parametrize("err, says", [
    ("APIConnectionError: Connection error.", "couldn't reach the model endpoint"),
    ("[WinError 10061] No connection could be made", "couldn't reach the model endpoint"),
    ("Error code: 503 - [stub/model] [429]: The usage limit has been reached (reset after 5m)",
     "out of quota"),
    ("Request timed out.", "didn't answer in time"),
    ("all models failed: empty reply", "answered with nothing"),
    ("_RouterDown: 9router is not running", "couldn't reach the model endpoint"),
    ("all models failed: rate-limited (reset after 5m)", "out of quota"),
])
def test_the_reply_says_what_went_wrong(err, says):
    out = bg._why_no_model(Exception(err))
    assert says in out and out.endswith((".", "!"))


def test_a_quota_failure_repeats_the_reset_time():
    out = bg._why_no_model(Exception("[429]: usage limit reached (reset after 7m 32s)"))
    assert "7m 32s" in out


def test_nothing_canned_is_left():
    src = open("cygnus/brain/core.py", encoding="utf-8").read()
    for gone in ("MockJarvisBrain", "_use_mock", "Opening Notepad for you",
                 "offline demo mode", "pre-scripted replies"):
        assert gone not in src, f"{gone} is back: a canned reply can claim an action that never ran"
