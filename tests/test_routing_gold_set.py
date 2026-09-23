"""The routing labelling tool, and the bug it found on its first run.

scripts/bench/routing.py mines real utterances out of data/sessions/ so the
"95% on routes that act" target is measured on what was actually said. Its
first run flagged two groups whose own phrasings routed differently - staccato
dictation with commas - which is the first test below.
"""

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

from cygnus.brain.intents import classify_intent

ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "routing_bench", ROOT / "scripts" / "bench" / "routing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("said, want", [
    ("Open, Telegram.", "app_reference"),
    ("open, notepad", "app_reference"),
    ("Force, close, brave.", "close_app"),
    ("close, chrome", "close_app"),
    ("Play, music.", "music"),
])
def test_dictation_commas_do_not_lose_the_action(said, want):
    """Whisper punctuates word-by-word speech; the action must survive it."""
    assert classify_intent(said) == want


@pytest.mark.parametrize("said", [
    "open notepad, chrome",                 # two targets, not staccato
    "close discord, mute the volume, then open spotify",
])
def test_real_commas_are_left_alone(said):
    """Only single-word pieces collapse, so listing targets still reads as a list."""
    from cygnus.brain import intents
    assert intents._destaccato(said.lower()) == said.lower()


def test_clusters_never_mix_two_routes():
    """"open chrome" and "close chrome" are one word apart and opposite acts."""
    tool = _load_tool()
    said = Counter({"open chrome": 5, "close chrome": 4, "open brave": 3,
                    "close brave": 2, "force close brave": 1})
    for group in tool.cluster(said, ratio=0.6):
        routes = {classify_intent(m) for m in group["members"]}
        assert len(routes) == 1, (group["rep"], group["members"], routes)


def test_answers_to_cygnus_are_not_mined(tmp_path, monkeypatch):
    """"yes" is an answer to a confirm, not a phrasing worth labelling."""
    tool = _load_tool()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"role": "user", "text": "yes"},
        {"role": "user", "text": "go ahead"},
        {"role": "assistant", "text": "open notepad"},   # not the user
        {"role": "user", "text": "open notepad"},
        {"role": "user", "text": "hi"},                  # one word
    ]), encoding="utf-8")
    monkeypatch.setattr(tool.paths, "SESSIONS_DIR", sessions)
    assert dict(tool.read_utterances()) == {"open notepad": 1}


def test_accept_holds_back_groups_that_route_inconsistently(tmp_path, monkeypatch):
    """A group the classifier disagrees with itself about has no usable proposal."""
    tool = _load_tool()
    review, gold = tmp_path / "review.jsonl", tmp_path / "gold.jsonl"
    review.write_text("\n".join(json.dumps(r) for r in [
        {"rep": "open telegram", "proposed": "app_reference", "label": None,
         "count": 3, "variants": 2, "agree": False,
         "routes": {"app_reference": 1, "general": 1},
         "examples": [], "members": ["open telegram"]},
        {"rep": "open notepad", "proposed": "app_reference", "label": None,
         "count": 9, "variants": 1, "agree": True, "routes": {"app_reference": 1},
         "examples": [], "members": ["open notepad"]},
        {"rep": "play lofi", "proposed": "general", "label": "music",
         "count": 4, "variants": 1, "agree": True, "routes": {"general": 1},
         "examples": [], "members": ["play lofi"]},
    ]), encoding="utf-8")
    monkeypatch.setattr(tool, "REVIEW", review)
    monkeypatch.setattr(tool, "GOLD", gold)

    class Args:
        all_variants = False
    assert tool.cmd_accept(Args()) == 0

    rows = {json.loads(l)["text"]: json.loads(l)
            for l in gold.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert "open telegram" not in rows          # held back for a person
    assert rows["open notepad"]["source"] == "proposed"
    assert rows["play lofi"]["label"] == "music"
    assert rows["play lofi"]["source"] == "confirmed"


def test_a_hand_set_label_is_not_overwritten_by_a_proposal(tmp_path, monkeypatch):
    tool = _load_tool()
    review, gold = tmp_path / "review.jsonl", tmp_path / "gold.jsonl"
    gold.write_text(json.dumps(
        {"text": "play lofi", "label": "music", "source": "confirmed"}) + "\n",
        encoding="utf-8")
    review.write_text(json.dumps(
        {"rep": "play lofi", "proposed": "general", "label": None, "count": 1,
         "variants": 1, "agree": True, "routes": {"general": 1},
         "examples": [], "members": ["play lofi"]}) + "\n", encoding="utf-8")
    monkeypatch.setattr(tool, "REVIEW", review)
    monkeypatch.setattr(tool, "GOLD", gold)

    class Args:
        all_variants = False
    tool.cmd_accept(Args())
    row = json.loads(gold.read_text(encoding="utf-8").splitlines()[0])
    assert row == {"text": "play lofi", "label": "music", "source": "confirmed"}


def test_intents_imports_on_its_own():
    """brain.intents used to need brain.core imported first (circular import)."""
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c",
         "from cygnus.brain.intents import classify_intent;"
         "print(classify_intent('open notepad'))"],
        cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-500:]
    assert "app_reference" in out.stdout
