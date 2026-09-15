"""agent_loop: the model reads the request with the tools, acts, sees the
result, decides again; leaves instant intents alone; checks what agents
return; hands back to the old routing when no model answers."""

import json
from types import SimpleNamespace as NS

import pytest

import agent_loop as al


class FakeBrain:
    def __init__(self):
        self.conversation = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "Hi."},
                             {"role": "user", "content": "the turn"}]
        self.last_backend = None


def call(name, args, cid="c1"):
    return NS(id=cid, function=NS(name=name, arguments=json.dumps(args)))


def reply(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


class FakeClient:
    """Scripted model answers; records what it was sent."""

    def __init__(self, script):
        self.script, self.sent = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.sent.append(kw)
        if not self.script:
            return reply("Done, sir.")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.setattr(al, "_models", lambda: ["fake"])
    monkeypatch.setattr(al, "_system", lambda ctx: "system")
    monkeypatch.setattr(al, "MAX_ROUNDS", 4)
    tools = []

    def fake_tool(name, args, ctx):
        tools.append((name, args))
        return {"whats_running": "Servers: web server on port 8788. Apps: Hermes desktop app.",
                "show_window": "Hermes desktop app is in front, sir.",
                "app_step": "Started a new Hermes session."}.get(name, f"ran {name}")
    monkeypatch.setattr(al, "_run_tool", fake_tool)

    def install(script):
        client = FakeClient(script)
        monkeypatch.setattr(al, "_make_client", lambda: client)
        return client
    install.tools = tools
    return install


def test_the_loop_acts_sees_the_result_and_answers(loop):
    client = loop([reply(tool_calls=[call("whats_running", {})]),
                   reply(tool_calls=[call("show_window", {"app": "hermes"})]),
                   reply("Hermes is in front, sir.")])
    b = FakeBrain()
    out = al.run(b, "display hermes", {"client": "c"})
    assert out == "Hermes is in front, sir."
    assert loop.tools == [("whats_running", {}), ("show_window", {"app": "hermes"})]
    assert client.sent[1]["messages"][-1]["role"] == "tool"          # the result went back to the model
    assert b.conversation[-1] == {"role": "assistant", "content": "Hermes is in front, sir."}
    assert al.last_stats()["tools"] == ["whats_running", "show_window"]


def test_a_confirm_from_a_tool_stops_the_loop_and_is_the_reply(loop, monkeypatch):
    monkeypatch.setattr(al, "_run_tool", lambda n, a, c: "[NEEDS_CONFIRM:ab12] Claude Code may change files. Say confirm.")
    loop([reply(tool_calls=[call("delegate", {"agent": "claude", "task": "fix it"})]), reply("should not be asked")])
    out = al.run(FakeBrain(), "fix the panel with claude", {"client": "c"})
    assert out.startswith("[NEEDS_CONFIRM:ab12]")


def test_no_model_answering_hands_the_turn_back(loop):
    loop([RuntimeError("503"), RuntimeError("503"), RuntimeError("503"), RuntimeError("503")])
    assert al.run(FakeBrain(), "display hermes", {"client": "c"}) is None


def test_a_claim_with_no_tool_run_is_not_relayed(loop):
    loop([reply("Opened Notepad for you, sir.")])
    out = al.run(FakeBrain(), "open notepad", {"client": "c"})
    assert out.startswith("I didn't do that yet, sir: nothing ran.")


def test_the_round_cap_ends_with_a_plain_summary(loop):
    loop([reply(tool_calls=[call("whats_running", {})])] * 4 + [reply("I listed what runs; nothing else was needed.")])
    out = al.run(FakeBrain(), "keep looking", {"client": "c"})
    assert out == "I listed what runs; nothing else was needed."
    assert al.last_stats()["rounds"] == 4


def test_a_delegated_answer_is_checked_before_relay(loop, monkeypatch):
    monkeypatch.setattr(al, "_run_tool", lambda n, a, c: '<atem:function_calls>\n<atem:invoke name="x">\n</atem:invoke>\n</atem:function_calls>')
    client = loop([reply(tool_calls=[call("delegate", {"agent": "hermes", "task": "t"})]), reply("Hermes didn't finish, sir.")])
    al.run(FakeBrain(), "ask hermes", {"client": "c"})
    assert client.sent[1]["messages"][-1]["content"].startswith("Hermes came back with tool markup and no answer")


def test_tidy_agent_output():
    assert al.tidy_agent_output("ERIC — need one clarification to avoid doing the wrong thing: which?", "Hermes") == \
        "Hermes answered as if to Eric, and asks: need one clarification to avoid doing the wrong thing: which?"
    assert al.tidy_agent_output("PONG", "Hermes") == "PONG"
    assert al.tidy_agent_output("All good.\n<function_calls><invoke name=\"a\"></invoke></function_calls>", "Codex") == "All good."


@pytest.mark.parametrize("text, intent, taken", [
    ("display hermes", "general", True), ("on the new session, prompt: what is musubae?", "general", True),
    ("open notepad", "app_reference", True), ("what time is it", "time", False), ("stop the music", "stop", False),
    ("/hermes hi", "general", False), ("ok", "general", False), ("what's running", "running", True)])
def test_which_turns_the_loop_takes(text, intent, taken):
    assert al.takes(text, intent) is taken


def test_the_app_in_play_is_remembered_and_told_to_the_model(monkeypatch):
    al._current_app.clear()
    import app_commands
    monkeypatch.setattr(app_commands, "targets", lambda rows=None, apps=None: [
        {"slug": "chrome", "name": "Google Chrome", "row": "1", "driver": "abilities", "kind": "window", "doing": ""}])
    import running_apps
    monkeypatch.setattr(running_apps, "inventory", lambda use_cache=True: [{"id": "1", "name": "Google Chrome",
                                                                            "group": "apps", "windows": [{"hwnd": 1}]}])
    monkeypatch.setattr(running_apps, "handle", lambda intent, text, remote=False: ("Google Chrome is in front, sir.", None))
    out = al._run_tool("show_window", {"app": "chrome"}, {"client": "phone"})
    assert out == "Google Chrome is in front, sir." and al._current_app["phone"] == "chrome"
    assert "The app in play from the last turns: chrome." in al._system({"client": "phone"})


def test_an_ability_marked_ask_is_asked_not_run(monkeypatch):
    import app_commands
    import dialogue_state
    import running_apps
    dialogue_state.reset()
    target = {"slug": "powerpoint", "name": "PowerPoint", "row": "20", "driver": "abilities", "kind": "window", "doing": ""}
    monkeypatch.setattr(app_commands, "targets", lambda rows=None, apps=None: [target])
    monkeypatch.setattr(running_apps, "inventory", lambda use_cache=True: [{"id": "20", "name": "PowerPoint", "group": "apps",
                                                                            "windows": [{"hwnd": 2}], "exes": ["powerpnt.exe"]}])
    monkeypatch.setattr(app_commands, "actions", lambda t, r, a, s=(): [
        {"do": "ability:powerpnt:powerpnt.save", "does": "Save", "args": {}, "ask": True}])
    ran = []
    monkeypatch.setattr(app_commands, "run", lambda slug, steps, rows=None, apps=None: ran.append(steps) or "ran")
    out = al._run_tool("app_step", {"app": "powerpoint", "do": "ability:powerpnt:powerpnt.save"}, {"client": "c"})
    assert out == "[Held] Should I save in PowerPoint, sir?" and ran == []
    assert dialogue_state.pending()["kind"] == "ability"
    dialogue_state.reset()
