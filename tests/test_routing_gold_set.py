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
        {"role": "user", "text": "/hermes reply with just the word PONG"},   # the "/" menu's path
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


def test_review_records_an_answer_and_saves_on_quit(tmp_path, monkeypatch):
    """Enter keeps the route, a number changes it, q writes what was answered."""
    tool = _load_tool()
    review = tmp_path / "review.jsonl"
    review.write_text("\n".join(json.dumps(r) for r in [
        {"rep": "stop the music", "proposed": "stop", "label": None, "count": 3,
         "variants": 1, "agree": True, "routes": {"stop": 1},
         "examples": ["stop the music"], "members": ["stop the music"]},
        {"rep": "open notepad", "proposed": "general", "label": None, "count": 2,
         "variants": 1, "agree": True, "routes": {"general": 1},
         "examples": ["open notepad"], "members": ["open notepad"]},
        {"rep": "play lofi", "proposed": "music", "label": None, "count": 1,
         "variants": 1, "agree": True, "routes": {"music": 1},
         "examples": ["play lofi"], "members": ["play lofi"]},
    ]), encoding="utf-8")
    monkeypatch.setattr(tool, "REVIEW", review)

    answers = iter(["", "1", "q"])        # right, wrong->menu[0], stop
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    class Args:
        resume = False
    assert tool.cmd_review(Args()) == 0

    rows = [json.loads(l) for l in review.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["label"] == "stop"            # agreeing is recorded too
    # "1" is the first menu entry: the most-proposed label in the file. All
    # three propose a different one, so ties keep file order: "stop".
    assert rows[1]["label"] == "stop" != rows[1]["proposed"]
    assert rows[2]["label"] is None              # never reached: q came first


def test_review_resume_only_asks_what_is_unanswered(tmp_path, monkeypatch):
    tool = _load_tool()
    review = tmp_path / "review.jsonl"
    review.write_text("\n".join(json.dumps(r) for r in [
        {"rep": "done one", "proposed": "stop", "label": "stop", "count": 1,
         "variants": 1, "agree": True, "routes": {"stop": 1},
         "examples": [], "members": ["done one"]},
        {"rep": "not yet", "proposed": "music", "label": None, "count": 1,
         "variants": 1, "agree": True, "routes": {"music": 1},
         "examples": [], "members": ["not yet"]},
    ]), encoding="utf-8")
    monkeypatch.setattr(tool, "REVIEW", review)
    asked = []

    def fake_input(*a):
        asked.append(1)
        return "q"
    monkeypatch.setattr("builtins.input", fake_input)

    class Args:
        resume = True
    tool.cmd_review(Args())
    assert len(asked) == 1, "resume re-asked a group that already had an answer"


def _group(rep, proposed, label=None, **extra):
    return {"rep": rep, "proposed": proposed, "label": label, "count": 1,
            "variants": 1, "agree": True, "routes": {proposed: 1},
            "examples": [rep], "members": [rep], **extra}


def test_a_note_leaves_the_group_unlabelled_and_accept_lists_it(tmp_path, monkeypatch, capsys):
    """[n]: none of the routes fit. The note is kept, and nothing is scored."""
    tool = _load_tool()
    review, gold = tmp_path / "review.jsonl", tmp_path / "gold.jsonl"
    review.write_text(json.dumps(_group("play it in spotify", "music")) + "\n", encoding="utf-8")
    monkeypatch.setattr(tool, "REVIEW", review)
    monkeypatch.setattr(tool, "GOLD", gold)
    answers = iter(["n", "it should have kept playing in Spotify"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))

    class Args:
        resume = False
        all_variants = False
    tool.cmd_review(Args())
    (row,) = [json.loads(l) for l in review.read_text(encoding="utf-8").splitlines()]
    assert row["label"] is None and row["note"] == "it should have kept playing in Spotify"

    capsys.readouterr()
    tool.cmd_accept(Args())
    assert gold.read_text(encoding="utf-8").strip() == ""          # not scored as agreement
    assert "it should have kept playing in Spotify" in capsys.readouterr().out


def test_resume_does_not_ask_a_noted_group_again(tmp_path, monkeypatch):
    tool = _load_tool()
    review = tmp_path / "review.jsonl"
    review.write_text("\n".join(json.dumps(r) for r in [
        _group("play it in spotify", "music", note="keep playing in Spotify"),
        _group("not yet", "music"),
    ]), encoding="utf-8")
    monkeypatch.setattr(tool, "REVIEW", review)
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "s")   # skip each one

    class Args:
        resume = True
    tool.cmd_review(Args())
    assert len(asked) == 1, "resume asked the noted group again"


def test_mining_again_keeps_the_answers(tmp_path, monkeypatch):
    """mine rewrote the review file and threw every answer away."""
    tool = _load_tool()
    sessions, review = tmp_path / "sessions", tmp_path / "review.jsonl"
    sessions.mkdir()
    (sessions / "s.jsonl").write_text("\n".join(json.dumps({"role": "user", "text": t}) for t in [
        "open notepad", "open notepad", "stop the music", "play some lofi",
    ]), encoding="utf-8")
    monkeypatch.setattr(tool.paths, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(tool, "REVIEW", review)
    review.write_text("\n".join(json.dumps(r) for r in [
        _group("open notepad", "app_reference", label="app_reference"),
        _group("play some lofi", "music", note="it should ask which playlist"),
    ]), encoding="utf-8")

    class Args:
        ratio = 0.7
        limit = 150
    tool.cmd_mine(Args())
    rows = {r["rep"]: r for r in (json.loads(l) for l in review.read_text(encoding="utf-8").splitlines())}
    assert rows["open notepad"]["label"] == "app_reference"
    assert rows["play some lofi"]["note"] == "it should ask which playlist"
    assert rows["stop the music"]["label"] is None


# The second mining run (2026-09-24, 2180 turns): groups whose route was wrong
# on its face. Each case is the owner's own phrasing from data/sessions/.

@pytest.mark.parametrize("said, want", [
    # Whisper hears "pause" as "post" or "pass"; it went to app_reference.
    ("JARVIS, post the music on Spotify, JARVIS.", "stop"),
    ("Post the music on Spotify.", "stop"),
    # "in the screen" is how "on the screen" is often said.
    ("display notepad in the screen, JARVIS", "running_front"),
    ("Display, open code in the screen.", "running_front"),
    # A one-word site, and a "can you" lead, are still visits.
    ("visit hollyhdmovies in chrome", "open_site"),
    ("Can you visit the Harley HD movies on Chrome?", "open_site"),
])
def test_mined_phrasings_that_act_route_to_the_act(said, want):
    assert classify_intent(said) == want


@pytest.mark.parametrize("said, not_this", [
    # "word" is Microsoft Word only when it is the thing being opened or used.
    ("In one word, what colour is the sky on a clear day?", "app_reference"),
    ("What files in this project reference the word 'delegate'?", "app_reference"),
    # A read or a summary with no page is not a page read.
    ("summarize in a paragraph what JARVIS can now do for me", "web_browse"),
    ("read my note called Task Lifecycle", "web_browse"),
    ("Now, can you read out loud the latest conversation?", "web_browse"),
    ("summarize this page", "web_browse"),          # no address: the reader refuses it
    ("can you create me a website with manus ai in web browser", "web_browse"),
    ("can you visit websites?", "open_site"),       # a question, not a visit
    # Saying "the report" is not asking for one.
    ("That is the report for now on most of the business.", "report"),
])
def test_mined_phrasings_that_do_not_act_are_not_misread(said, not_this):
    assert classify_intent(said) != not_this


@pytest.mark.parametrize("said, want", [
    ("open microsoft word", "app_reference"),
    ("can you write hello world on the microsoft word?", "app_reference"),
    ("open word", "app_reference"),
    ("scrape https://example.com and tell me the page title", "web_browse"),
    ("summarize https://example.com/post", "web_browse"),
    ("read the article on theverge.com", "web_browse"),
    ("research WebGPU and write me a short report", "report"),
    ("Compose a report about the AI assistant work we did", "report"),
    ("create me a summary report for musubae", "report"),
    ("play some music on spotify", "music"),
    ("go to sleep", "general"),
    ("post this on facebook", "general"),
])
def test_what_those_fixes_leave_alone(said, want):
    assert classify_intent(said) == want


@pytest.mark.parametrize("said, app", [
    ("display notepad in the screen, JARVIS", "notepad"),
    ("Display, open code in the screen.", "open code"),
    ("show on screen PowerPoint", "PowerPoint"),
    ("display hermes on the screen", "hermes"),
])
def test_the_window_named_is_the_app_alone(said, app):
    from cygnus.apps import running
    assert running.app_phrase(said) == app


def test_accept_rebuilds_proposals_and_keeps_confirmed_labels(tmp_path, monkeypatch):
    """A stale proposal outlived the classifier fix and was scored as a miss."""
    tool = _load_tool()
    review, gold = tmp_path / "review.jsonl", tmp_path / "gold.jsonl"
    gold.write_text("".join(json.dumps(r) + "\n" for r in [
        {"text": "In one word, what colour is the sky?", "label": "app_reference", "source": "proposed"},
        {"text": "play lofi", "label": "music", "source": "confirmed"},
    ]), encoding="utf-8")
    review.write_text(json.dumps(_group("open notepad", "app_reference")) + "\n", encoding="utf-8")
    monkeypatch.setattr(tool, "REVIEW", review)
    monkeypatch.setattr(tool, "GOLD", gold)

    class Args:
        all_variants = False
    tool.cmd_accept(Args())
    rows = {json.loads(l)["text"]: json.loads(l)["source"]
            for l in gold.read_text(encoding="utf-8").splitlines() if l.strip()}
    assert rows == {"play lofi": "confirmed", "open notepad": "proposed"}


# The owner's review (2026-09-25): answers that disagreed with the route.

@pytest.mark.parametrize("said, want", [
    ("Can you go to the link, iRIMSV.net?", "open_site"),
    ("go to the url irimsv.net", "open_site"),
    ("try facebook.com", "open_site"),
    # "pass the music" is play, not pause - the owner's own note.
    ("Pass the music on Brave.", "music"),
    ("Pass the music on a brave.", "music"),
    # confirmed a pause
    ("Post the music on Spotify, JARVIS.", "stop"),
])
def test_what_the_owner_said_it_meant(said, want):
    assert classify_intent(said) == want


@pytest.mark.parametrize("said", [
    "Open chat GPT in search in history, iRIMS-V project, report.",
    "open chatgpt and search in history irimsv project report then write in microsoft word",
])
def test_a_project_further_on_is_not_the_thing_opened(said):
    assert classify_intent(said) != "launch"


@pytest.mark.parametrize("said, want", [
    ("Launch the iRIMS project", "launch"),
    ("open irimsv project", "launch"),
    ("open project path irimsv", "launch"),
    ("start the dev server", "launch"),
    ("try again", "general"),
    ("try it", "clarify_open"),
])
def test_launch_and_try_still_mean_what_they_did(said, want):
    assert classify_intent(said) == want
