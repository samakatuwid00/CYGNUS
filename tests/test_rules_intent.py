"""Rules that understand intent (RULES_INTENT_PLAN.md): the compiler reads
every phrase with context, reports gaps, keeps conditions; the semantic tier
matches natural speech and learns triggers; the spoken path checks
conditions, asks "did you mean", and reworks a rule on "that didn't work".
The model is always faked."""
import datetime
import json

import pytest

import dialogue_state
import rules_ai
import rules_engine
import rules_match
import rules_voice
import tools


def _fake_llm(reply, seen=None):
    def llm(prompt, **kw):
        if seen is not None:
            seen.append(prompt)
        return (dict(reply), "fake-model") if reply is not None else (None, "down")
    return llm


def _registry(tmp_path, apps):
    path = tmp_path / "app_registry.json"
    path.write_text(json.dumps({"apps": apps}), encoding="utf-8")
    return path


SPOTIFY = {"name": "Spotify", "category": "media", "bin": "C:\\\\spotify.exe",
           "abilities": [{"id": "spotify.play_pause", "name": "Play or pause", "source": "template"},
                         {"id": "spotify.scan.1", "name": "Workout", "source": "scan"}]}


# ------------------------------------------------------------- compile --

def test_prompt_carries_app_controls_and_existing_rules():
    seen = []
    entry = dict(SPOTIFY, compiled_rules=[{"source_phrase": "gym time", "goal": "play workout"}])
    rules_ai.compile_rule("spotify", "chill time", "play my chill playlist",
                          entry=entry, llm=_fake_llm({"type": "reminder"}, seen))
    assert "KNOWN CONTROLS OR ABILITIES" in seen[0]
    assert "Play or pause" in seen[0] and "Workout" in seen[0]
    assert '"gym time" -> play workout' in seen[0]


def test_well_known_site_name_is_accepted_from_the_model():
    reply = {"type": "steps", "site": "music.youtube.com", "browser": "brave",
             "steps": [{"op": "open"}, {"op": "click", "target": "Shuffle"}],
             "goal": "play my music", "expects": "music is playing"}
    rule, _ = rules_ai.compile_rule("brave", "play music", "open youtube music and shuffle",
                                    entry={"category": "browser"}, llm=_fake_llm(reply))
    assert rule["action"]["site"] == "music.youtube.com"
    assert rule["needs_clarification"] is False
    assert rule["expects"] == "music is playing"
    assert rule["goal"] == "play my music"


def test_invented_site_is_still_dropped():
    reply = {"type": "open_site", "site": "evil.example"}
    rule, _ = rules_ai.compile_rule("brave", "open my stuff", "open it",
                                    entry={"category": "browser"}, llm=_fake_llm(reply))
    assert not rule["action"].get("site")
    assert rule["needs_clarification"] is True


def test_dropped_key_step_is_a_gap_not_a_silent_reminder():
    reply = {"type": "steps", "steps": [{"op": "key", "keys": "{LWIN}"}]}
    rule, _ = rules_ai.compile_rule("notepad", "lock it", "press the windows key",
                                    entry={"bin": "notepad.exe"}, llm=_fake_llm(reply))
    assert rule["action"]["type"] == "reminder"
    assert any("LWIN" in g for g in rule["gaps"])
    assert any("became a reminder" in g for g in rule["gaps"])


def test_ctrl_s_key_step_survives():
    reply = {"type": "steps", "steps": [{"op": "key", "keys": "^s"}], "goal": "save the file"}
    rule, _ = rules_ai.compile_rule("notepad", "save it", "press ctrl s",
                                    entry={"bin": "notepad.exe"}, llm=_fake_llm(reply))
    assert rule["action"]["type"] == "steps"
    assert rule["action"]["steps"] == [{"op": "key", "keys": "^s"}]
    assert rule["gaps"] == []


def test_unknown_desktop_click_target_is_reported():
    reply = {"type": "steps", "steps": [{"op": "launch"}, {"op": "click", "target": "Liked Songs"}]}
    rule, _ = rules_ai.compile_rule("spotify", "my likes", "open spotify and click liked songs",
                                    entry=SPOTIFY, llm=_fake_llm(reply))
    assert rule["action"]["steps"][1]["target"] == "Liked Songs"
    assert any("Liked Songs" in g for g in rule["gaps"])
    reply["steps"][1]["target"] = "Workout"
    rule, _ = rules_ai.compile_rule("spotify", "gym time", "open spotify and click workout",
                                    entry=SPOTIFY, llm=_fake_llm(reply))
    assert rule["gaps"] == []


def test_time_condition_is_kept_and_other_conditions_become_gaps():
    reply = {"type": "block", "applies_to": "launch",
             "condition": {"kind": "time_after", "start": "23:00"}}
    rule, _ = rules_ai.compile_rule("spotify", "never open spotify after 11pm", "",
                                    entry=SPOTIFY, llm=_fake_llm(reply))
    assert rule["condition"] == {"kind": "time_after", "start": "23:00"}
    assert "only after 23:00" in rule["summary"]
    reply = {"type": "block", "applies_to": "close",
             "condition": {"kind": "other", "text": "without saving"}}
    rule, _ = rules_ai.compile_rule("notepad", "never close notepad without saving", "",
                                    entry={"bin": "notepad.exe"}, llm=_fake_llm(reply))
    assert rule["condition"] is None
    assert any("without saving" in g for g in rule["gaps"])


def test_proposal_markdown_lists_condition_expects_and_gaps():
    rule = {"summary": "x", "action": {"type": "block"}, "enforcement": "hard",
            "condition": {"kind": "time_between", "start": "22:00", "end": "07:00"},
            "expects": "nothing opens", "gaps": ["I can't check meetings yet"]}
    md = rules_ai.propose_markdown("spotify", [rule])
    assert "only between 22:00 and 07:00" in md
    assert "done when: nothing opens" in md
    assert "can't yet: I can't check meetings yet" in md


# ----------------------------------------------------------- authoring --

def test_preference_phrase_reaches_the_ai_compiler(tmp_path, monkeypatch):
    path = _registry(tmp_path, {"spotify": SPOTIFY})
    seen = []
    reply = {"type": "reminder", "adapter_check": "pre_play: cap spotify volume at 40%",
             "goal": "keep spotify quiet"}
    monkeypatch.setattr(rules_ai, "_ask_llm", _fake_llm(reply, seen))
    step = rules_voice.begin_rule_setup("spotify", "keep spotify under 40% volume",
                                        registry_path=str(path))
    assert step["status"] == "proposal"
    assert seen, "the model was not asked"
    assert step["proposed"][0]["adapter_check"] == "pre_play: cap spotify volume at 40%"
    rules_voice.cancel_setup("spotify")


def test_no_model_keeps_the_keyword_scaffold(tmp_path, monkeypatch):
    path = _registry(tmp_path, {"spotify": SPOTIFY})
    monkeypatch.setattr(rules_ai, "_ask_llm", _fake_llm(None))
    step = rules_voice.begin_rule_setup("spotify", "no explicit songs", registry_path=str(path))
    assert step["status"] == "proposal"
    assert step["proposed"][0]["adapter_check"] == "pre_play: skip if track.explicit"
    rules_voice.cancel_setup("spotify")


def test_proposal_question_names_the_gaps(tmp_path, monkeypatch):
    path = _registry(tmp_path, {"notepad": {"bin": "notepad.exe"}})
    reply = {"type": "steps", "steps": [{"op": "key", "keys": "{LWIN}"}]}
    monkeypatch.setattr(rules_ai, "_ask_llm", _fake_llm(reply))
    step = rules_voice.begin_rule_setup("notepad", "lock it -> press the windows key",
                                        registry_path=str(path))
    assert step["status"] == "proposal"
    assert "Heads up" in step["question"] and "LWIN" in step["question"]
    rules_voice.cancel_setup("notepad")


# ------------------------------------------------------------ conditions --

def test_condition_holds_by_clock():
    at = lambda h, m=0: datetime.datetime(2026, 9, 15, h, m)
    after = {"condition": {"kind": "time_after", "start": "23:00"}}
    assert rules_engine.condition_holds(after, at(23, 30))
    assert not rules_engine.condition_holds(after, at(15))
    between = {"condition": {"kind": "time_between", "start": "22:00", "end": "07:00"}}
    assert rules_engine.condition_holds(between, at(2))
    assert not rules_engine.condition_holds(between, at(12))
    assert rules_engine.condition_holds({"condition": None}, at(12))


def test_gate_skips_a_block_outside_its_time(monkeypatch):
    rule = {"rule_id": "r", "enforcement": "hard", "source_phrase": "never open after 11pm",
            "condition": {"kind": "time_after", "start": "23:00"}}
    monkeypatch.setattr(rules_engine, "condition_holds", lambda r, now=None: False)
    assert rules_engine.check("spotify", {"entry": {"compiled_rules": [rule]}})["action"] == "allow"
    monkeypatch.setattr(rules_engine, "condition_holds", lambda r, now=None: True)
    assert rules_engine.check("spotify", {"entry": {"compiled_rules": [rule]}})["action"] == "deny"


# ------------------------------------------------------------- semantic --

GYM = {"rule_id": "gym_time", "source_phrase": "gym time", "triggers": ["gym time"],
       "goal": "play the workout playlist on spotify", "enforcement": "action",
       "expects": "music is playing",
       "action": {"type": "steps", "steps": [{"op": "launch"}, {"op": "click", "target": "Workout"}]}}
MSG = {"rule_id": "message_jd", "source_phrase": "message jd", "triggers": ["message jd"],
       "goal": "send jd a telegram message", "enforcement": "action",
       "action": {"type": "steps", "slot": "message",
                  "steps": [{"op": "launch"}, {"op": "type", "target": "box", "text": "{query}"}]}}
APPS = {"spotify": dict(SPOTIFY, compiled_rules=[GYM]),
        "telegram": {"name": "Telegram", "bin": "t.exe", "compiled_rules": [MSG]}}


def test_semantic_picks_the_rule_by_meaning_with_slot():
    seen = []
    llm = _fake_llm({"rule": "telegram/message_jd", "slot": "I'm late", "confidence": 0.9}, seen)
    m = rules_match.semantic("tell jd I'm late", apps=APPS, llm=llm, front="spotify")
    assert m["owner"] == "telegram" and m["rule"]["rule_id"] == "message_jd"
    assert m["slot"] == "I'm late" and m["confidence"] == 0.9
    # the app in front is listed first, every rule shows its goal
    assert seen[0].index("spotify/gym_time") < seen[0].index("telegram/message_jd")
    assert "send jd a telegram message" in seen[0]


def test_semantic_skips_short_and_null_answers():
    assert rules_match.semantic("yes", apps=APPS, llm=_fake_llm({"rule": "spotify/gym_time"})) is None
    assert rules_match.semantic("what time is it", apps=APPS,
                                llm=_fake_llm({"rule": None, "confidence": 0})) is None
    assert rules_match.semantic("play music", apps={}, llm=_fake_llm({"rule": "x/y"})) is None


def test_learn_trigger_writes_the_registry(tmp_path):
    path = _registry(tmp_path, APPS)
    assert rules_match.learn_trigger("spotify", "gym_time", "It's gym time!", registry_path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "it s gym time" in data["apps"]["spotify"]["compiled_rules"][0]["triggers"]
    assert not rules_match.learn_trigger("spotify", "gym_time", "gym time", registry_path=path)


# ------------------------------------------------------------ spoken path --

@pytest.fixture
def spoken(monkeypatch, tmp_path):
    path = _registry(tmp_path, APPS)
    monkeypatch.setattr(rules_engine, "APP_REGISTRY_PATH", str(path))
    monkeypatch.setattr(tools, "_RULE_GUESS", {})
    monkeypatch.setattr(tools, "_LAST_RULE_RUN", {})
    monkeypatch.setattr(tools, "_RULE_FIX", {})
    monkeypatch.setattr(tools, "_EXPECT_HINTS", {})
    ran = []
    import rules_steps
    monkeypatch.setattr(rules_steps, "start", lambda rule, owner, action, slot="", entry=None:
                        ran.append((rule["rule_id"], slot)) or "Opened the app.")
    return path, ran


def test_sure_semantic_match_runs_and_learns(spoken, monkeypatch):
    path, ran = spoken
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: {
        "rule": GYM, "owner": "spotify", "action": rules_engine.action_of(GYM, "spotify", APPS["spotify"]),
        "slot": "", "confidence": 0.9, "entry": APPS["spotify"], "trigger": None})
    out = tools.try_rule_action("it's gym time")
    assert ran == [("gym_time", "")]
    assert out.startswith("Opened the app.")
    assert "You should see: music is playing" in out
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "it s gym time" in data["apps"]["spotify"]["compiled_rules"][0]["triggers"]


def test_unsure_match_asks_and_a_yes_runs(spoken, monkeypatch):
    path, ran = spoken
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: {
        "rule": GYM, "owner": "spotify", "action": rules_engine.action_of(GYM, "spotify", APPS["spotify"]),
        "slot": "", "confidence": 0.5, "entry": APPS["spotify"], "trigger": None})
    out = tools.try_rule_action("let's hit the gym")
    assert out == 'Did you mean your "gym time" rule (spotify)?'
    assert ran == []
    assert dialogue_state.pending("rule_guess")
    out = tools.try_rule_action("yes")
    assert ran == [("gym_time", "")]
    assert dialogue_state.pending("rule_guess") is None


def test_unsure_match_no_leaves_it(spoken, monkeypatch):
    path, ran = spoken
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: {
        "rule": GYM, "owner": "spotify", "action": {"type": "steps", "steps": []},
        "slot": "", "confidence": 0.5, "entry": {}, "trigger": None})
    tools.try_rule_action("let's hit the gym")
    assert tools.try_rule_action("no") == "Okay, I'll leave it."
    assert ran == []


def test_condition_blocks_a_spoken_rule_with_a_reason(spoken, monkeypatch):
    path, ran = spoken
    late = dict(GYM, condition={"kind": "time_after", "start": "23:00"})
    monkeypatch.setattr(rules_engine, "condition_holds", lambda r, now=None: False)
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: {
        "rule": late, "owner": "spotify", "action": rules_engine.action_of(late, "spotify", APPS["spotify"]),
        "slot": "", "confidence": 0.9, "entry": APPS["spotify"], "trigger": None})
    out = tools.try_rule_action("it's gym time")
    assert "only applies after 23:00" in out
    assert ran == []


def test_that_didnt_work_reworks_the_rule(spoken, monkeypatch):
    path, ran = spoken
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: None)
    assert tools.try_rule_action("gym time").startswith("Opened the app.")
    reply = {"type": "steps", "goal": "play workout", "steps": [
        {"op": "launch"}, {"op": "click", "target": "Workout"}, {"op": "click", "target": "Play"}]}
    monkeypatch.setattr(rules_ai, "_ask_llm", _fake_llm(reply))
    import machine_capabilities
    monkeypatch.setattr(machine_capabilities, "REGISTRY_PATH", str(path))
    out = tools.try_rule_action("that didn't work, it should press play too")
    assert out.startswith("Here's a new version:")
    assert "Say yes to save it" in out
    assert dialogue_state.pending("rule_fix")
    assert tools.try_rule_action("yes") == "Saved. The rule now does that."
    data = json.loads(path.read_text(encoding="utf-8"))
    rules = data["apps"]["spotify"]["compiled_rules"]
    assert [r["rule_id"] for r in rules] == ["gym_time"]
    assert rules[0]["action"]["steps"][-1]["target"] == "Play"


def test_that_didnt_work_without_detail_asks_first(spoken, monkeypatch):
    path, ran = spoken
    monkeypatch.setattr(rules_match, "semantic", lambda t, **kw: None)
    tools.try_rule_action("gym time")
    out = tools.try_rule_action("that didn't work")
    assert out.startswith("Sorry, sir.") and "what should have happened" in out
    assert tools.try_rule_action("never mind") == "Okay, the rule stays as it is."
    assert dialogue_state.pending("rule_fix") is None
