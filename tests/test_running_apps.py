"""running_apps: what is running, grouped like Task Manager, and what each
program is doing - from fake process lists and sample transcript lines."""

import json

import pytest

import running_apps as ra

NOW = 1_800_000_000.0


def proc(pid, name, cmd, ppid=1, exe=None, started=None, cpu=5.0):
    return {"pid": pid, "ppid": ppid, "name": name, "exe": exe or f"C:\\bin\\{name}",
            "cmdline": cmd.split(), "started": NOW - 600 if started is None else started,
            "cpu_s": cpu, "mem_mb": 50.0}


def rows_of(procs, windows=None, ports=None, jobs=()):
    return ra.build_rows({p["pid"]: p for p in procs}, windows or {}, ports or {}, NOW, jobs)


@pytest.fixture(autouse=True)
def no_real_readers(monkeypatch):
    # Interactive sessions have a transcript here; one-off runs do not.
    monkeypatch.setattr(ra, "_claude_sessions", lambda ps, now: {
        p["pid"]: {"folder": "jarvis-demo", "status": "working", "doing": "editing tools.py",
                   "detail": [], "matched": True}
        for p in ps if "-p" not in p["cmdline"]})
    monkeypatch.setattr(ra, "_hermes_for", lambda now: {"status": "idle", "doing": "idle for 5 minutes",
                                                          "detail": []})


# --------------------------------------------------------------- grouping --

def test_a_launcher_and_its_child_are_one_server():
    cmd = "python -m http.server 8787 --directory C:\\sites\\demo"
    rows = rows_of([proc(10, "python.exe", cmd, exe="C:\\Users\\x\\WindowsApps\\python.exe"),
                    proc(11, "python.exe", cmd, ppid=10)], ports={11: [8787]})
    assert [(r["group"], r["name"], r["doing"], r["flags"]) for r in rows] == \
        [("servers", "web server", "web server on port 8787 serving demo", [])]


def test_a_launcher_chain_belongs_to_the_server_it_started():
    rows = rows_of([proc(60, "bash.exe", "bash npm run dev", started=NOW - 2 * 86400, cpu=0.1),
                    proc(61, "node.exe", "node C:\\npm\\npm-cli.js run dev", ppid=60),
                    proc(62, "sh.exe", "sh C:\\app\\node_modules\\.bin\\vite", ppid=61),
                    proc(63, "node.exe", "node C:\\app\\node_modules\\vite\\bin\\vite.js", ppid=62)],
                   ports={63: [4173]})
    assert [(r["group"], r["name"], r["doing"], r["flags"]) for r in rows] == \
        [("servers", "Vite", "vite on port 4173", [])]


def test_a_vendor_service_on_a_port_is_not_a_server():
    assert rows_of([proc(70, "OneDrive.Sync.Service.exe", "OneDrive.Sync.Service.exe")], ports={70: [42050]}) == []


@pytest.mark.parametrize("cmd, name, agent", [
    ("tail -n0 -F /c/Users/x/AppData/Local/hermes/logs/agent.log", "tail.exe", None),
    ("grep -E --line-buffered ws closed|opencode-smoke", "grep.exe", None),
    ("python -m hermes_cli.main gateway run", "python.exe", "Hermes gateway"),
    ("python C:\\Users\\x\\hermes\\mcp_server.py", "python.exe", "Hermes helper")])
def test_agents_are_known_by_the_program_they_run(cmd, name, agent):
    assert ra._agent_name(proc(1, name, cmd)) == agent


def test_the_same_server_twice_is_a_duplicate():
    cmd = "python -m http.server 8788"
    rows = rows_of([proc(10, "python.exe", cmd), proc(20, "python.exe", cmd)], ports={10: [8788], 20: [8788]})
    assert [r["flags"] for r in rows] == [["duplicate"], ["duplicate"]]


def test_a_forgotten_watcher_is_a_leftover():
    rows = rows_of([proc(10, "tail.exe", "tail -n0 -F /c/hermes/logs/agent.log",
                         started=NOW - 3 * 86400, cpu=0.2)])
    assert rows[0]["group"] == "background" and rows[0]["flags"] == ["leftover"]
    assert rows[0]["since"].startswith("since ")


def test_an_agents_shells_roll_up_but_the_agents_it_starts_do_not():
    rows = rows_of([proc(20, "claude.exe", "claude", exe="C:\\Users\\x\\.local\\bin\\claude.exe"),
                    proc(21, "bash.exe", "bash -c ls", ppid=20),
                    proc(22, "claude.exe", "claude -p --model haiku", ppid=21,
                         exe="C:\\Users\\x\\.local\\bin\\claude.exe"),
                    proc(23, "python.exe", "python check.py", ppid=21)])
    assert [(r["name"], r["doing"]) for r in rows] == [
        ("Claude Code in jarvis-demo", "editing tools.py"), ("Claude one-off run", "one-off run on haiku")]


def test_each_claude_process_gets_the_transcript_it_writes():
    old, new = (1, NOW - 12 * 3600), (2, NOW - 120)
    files = [("mine.jsonl", NOW - 12 * 3600 + 5, NOW - 10), ("run.jsonl", NOW - 100, NOW - 5)]
    assert ra.match_transcripts([old, new], files) == {2: "run.jsonl", 1: "mine.jsonl"}
    resumed = [("yesterday.jsonl", NOW - 86400, NOW - 30)]           # created before it started
    assert ra.match_transcripts([(3, NOW - 600)], resumed) == {3: "yesterday.jsonl"}
    assert ra.match_transcripts([(4, NOW - 60)], [("stale.jsonl", NOW - 86400, NOW - 3600)]) == {}


def test_a_one_off_run_with_a_transcript_says_what_it_is_doing(monkeypatch):
    monkeypatch.setattr(ra, "_claude_sessions", lambda ps, now: {p["pid"]: {
        "folder": "jarvis-demo", "status": "working", "doing": "editing _probe.js",
        "detail": ["Session: Responsive HUD overhaul"], "matched": True} for p in ps})
    rows = rows_of([proc(1, "python.exe", "python jarvis_web.py", ppid=0),
                    proc(50, "claude.exe", "claude -p --model sonnet", ppid=1)], ports={1: [8001]})
    run = next(r for r in rows if r["name"].startswith("Claude one-off"))
    assert (run["name"], run["doing"]) == ("Claude one-off run in jarvis-demo", "editing _probe.js")
    assert run["detail"][:2] == ["Model: sonnet", "Session: Responsive HUD overhaul"]
    assert "Started by Cygnus" in run["detail"] and "Started by python.exe" not in run["detail"]


def test_a_windowed_app_is_named_from_its_title_and_the_desktop_claude_is_an_agent():
    rows = rows_of([proc(30, "Code.exe", "Code.exe"),
                    proc(40, "claude.exe", "claude.exe", exe="C:\\Program Files\\WindowsApps\\Claude_1\\claude.exe"),
                    proc(41, "claude.exe", "claude.exe --type=renderer", ppid=40,
                         exe="C:\\Program Files\\WindowsApps\\Claude_1\\claude.exe")],
                   windows={30: ["tools.py - jarvis-demo - Visual Studio Code"], 40: ["Claude"]})
    assert [(r["group"], r["name"], r["doing"]) for r in rows] == [
        ("agents", "Claude desktop app", "open"), ("apps", "Visual Studio Code", "tools.py - jarvis-demo")]


def test_an_overlay_window_is_not_an_app():
    assert rows_of([proc(90, "cua-driver.exe", "cua-driver.exe")],
                   windows={90: ["Cua.AgentCursorOverlay.default"]}) == []


def test_cygnus_tasks_are_agents_and_what_cygnus_started_says_so():
    rows = rows_of([proc(1, "python.exe", "python jarvis_web.py", ppid=0),
                    proc(50, "claude.exe", "claude -p --model sonnet", ppid=1)], ports={1: [8001]},
                   jobs=[{"id": "ab12", "task": "fix the chat box", "state": "waiting-on-confirm", "progress": []}])
    names = {r["name"]: r for r in rows}
    assert names["Cygnus task"]["status"] == "waiting" and "waiting for you" not in names["Cygnus task"]["flags"]
    assert "Started by Cygnus" in names["Claude one-off run"]["detail"]
    assert names["Cygnus backend"]["doing"] == "Cygnus's own backend on port 8001"


def test_electron_apps_are_named_by_their_window():
    rows = rows_of([proc(80, "electron.exe", "electron.exe"), proc(81, "WindowsTerminal.exe", "WindowsTerminal.exe")],
                   windows={80: ["Cygnus"], 81: ["Cygnus wake word not working"]})
    assert sorted((r["name"], r["doing"]) for r in rows) == [
        ("Cygnus", "open"), ("Windows Terminal", "Cygnus wake word not working")]


# ------------------------------------------------------------------ readers --

def line(kind, ts, **extra):
    return json.dumps(dict({"type": kind, "timestamp": ts}, **extra))


def iso(seconds_ago):
    import datetime
    return datetime.datetime.fromtimestamp(NOW - seconds_ago, datetime.timezone.utc).isoformat()


def test_a_claude_session_mid_edit_is_working():
    lines = [line("ai-title", iso(300), aiTitle="Fix the HUD"),
             line("last-prompt", iso(300), lastPrompt="fix the chat box"),
             line("assistant", iso(20), message={"content": [
                 {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:\\r\\tools.py"}}]})]
    s = ra.claude_session(lines, NOW)
    assert (s["status"], s["doing"]) == ("working", "editing tools.py")
    assert s["detail"] == ["Session: Fix the HUD", "Last prompt: fix the chat box", "Then: editing tools.py"]


def test_a_claude_session_that_answered_waits_then_goes_idle():
    replied = [line("assistant", iso(60), message={"content": [{"type": "text", "text": "Done."}]})]
    assert ra.claude_session(replied, NOW)["status"] == "waiting"
    assert ra.claude_session(replied, NOW + ra.WAITING_S)["status"] == "idle"


def test_a_tool_call_unanswered_for_half_an_hour_is_stuck():
    lines = [line("assistant", iso(ra.STUCK_S + 60), message={"content": [
        {"type": "tool_use", "name": "Bash", "input": {"description": "run the tests"}}]})]
    assert ra.claude_session(lines, NOW)["status"] == "stuck"


def test_hermes_is_idle_once_its_turn_ended():
    import time
    t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - 300))
    s = ra.hermes_status([f"{t},960 INFO [s1] agent.conversation_loop: Turn ended: reason=stop"], NOW)
    assert (s["status"], s["doing"]) == ("idle", "idle for 5 minutes")


def test_hermes_works_only_while_it_calls_models_or_tools():
    import time
    stamp = lambda ago: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW - ago))
    busy = [f"{stamp(30)},100 INFO [s2] agent.conversation_loop: API call #2: model=x"]
    assert ra.hermes_status(busy, NOW)["status"] == "working"
    chatter = [f"{stamp(900)},100 INFO [s2] agent.conversation_loop: Turn ended: reason=stop",
               f"{stamp(20)},100 INFO tools.terminal_tool: Cleaned up inactive environment"]
    assert ra.hermes_status(chatter, NOW)["status"] == "idle"


# ---------------------------------------------------------------- speaking --

def test_the_spoken_summary():
    web = "python -m http.server 8788 --directory C:\\sites\\notes"
    rows = rows_of([proc(20, "claude.exe", "claude", exe="C:\\Users\\x\\.local\\bin\\claude.exe"),
                    proc(40, "claude.exe", "claude.exe", exe="C:\\Program Files\\WindowsApps\\Claude_1\\claude.exe"),
                    proc(10, "python.exe", web), proc(11, "python.exe", web),
                    proc(12, "tail.exe", "tail -F agent.log", started=NOW - 3 * 86400, cpu=0.1),
                    proc(30, "Code.exe", "Code.exe")],
                   windows={30: ["tools.py - jarvis-demo - Visual Studio Code"], 40: ["Claude"]},
                   ports={10: [8788], 11: [8788]})
    text = ra.summary(rows)
    assert text.startswith("Agents: Claude Code in jarvis-demo, editing tools.py. Also open: Claude desktop app.")
    assert "Servers: web server on port 8788 serving notes." in text
    assert "Worth a look: two copies of the web server on port 8788 serving notes and tail left running since" in text
    assert text.endswith("Plus 1 app with windows, like Visual Studio Code.")
    assert ra.summary(rows, focus="claude") == "Claude Code in jarvis-demo, editing tools.py; and Claude desktop app."
    assert ra.summary(rows, focus="hermes") == "I don't see Hermes running, sir."
    assert ra.summary([]) == "Nothing of note is running, sir."


def test_explain_context_carries_the_agents_steps():
    rows = rows_of([proc(20, "claude.exe", "claude", exe="C:\\Users\\x\\.local\\bin\\claude.exe")])
    assert ra.explain_context(rows, "claude").startswith("Claude Code in jarvis-demo: working, editing tools.py")


@pytest.mark.parametrize("text, intent", [
    ("what's running?", "running"), ("what apps are running on my laptop", "running"),
    ("what is claude doing", "running"), ("explain what claude is doing", "running_explain"),
    ("what's the status?", "job_status"), ("what apps do I have", "list_installed")])
def test_routing(text, intent):
    import brain_gemini
    assert brain_gemini.classify_intent(text) == intent
