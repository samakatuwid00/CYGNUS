"""Agents installed as npm .cmd shims start from the shim's real target, not
through cmd.exe, which cut every OpenCode task at its first line break and
reads & | " in it as commands (2026-09-13)."""

import shutil
import subprocess

import pytest

from cygnus import tools

EXE_SHIM = '@ECHO off\nSETLOCAL\n"%dp0%\\node_modules\\opencode-ai\\bin\\opencode.exe"   %*\n'
NODE_SHIM = ('IF EXIST "%dp0%\\node.exe" (\n  SET "_prog=%dp0%\\node.exe"\n)\n'
             '"%_prog%"  "%dp0%\\node_modules\\@google\\gemini-cli\\bundle\\{js}" %*\n')


def _shim(tmp_path, monkeypatch, text, *files):
    shim = tmp_path / "agent.CMD"
    shim.write_text(text)
    for rel in files:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("")
    real_which = shutil.which
    monkeypatch.setattr(tools.shutil, "which", lambda b: str(shim) if b == "agent" else real_which(b))
    return tmp_path


def test_a_shim_for_an_exe_runs_the_exe(tmp_path, monkeypatch):
    d = _shim(tmp_path, monkeypatch, EXE_SHIM, "node_modules/opencode-ai/bin/opencode.exe")
    assert tools._direct_launcher("agent") == [str(d / "node_modules/opencode-ai/bin/opencode.exe")]


def test_a_shim_for_a_script_runs_node_on_it(tmp_path, monkeypatch):
    js = "node_modules/@google/gemini-cli/bundle/gemini.js"
    d = _shim(tmp_path, monkeypatch, NODE_SHIM.format(js="gemini.js"), js, "node.exe")
    assert tools._direct_launcher("agent") == [str(d / "node.exe"), str(d / js)]


def test_a_shim_it_cannot_read_is_refused_not_run_through_cmd(tmp_path, monkeypatch):
    _shim(tmp_path, monkeypatch, "@ECHO off\ncall something-else %*\n")
    assert tools._direct_launcher("agent") is None


def test_a_real_exe_is_used_as_it_is(monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda b: "C:/bin/claude.exe")
    assert tools._direct_launcher("claude") == ["C:/bin/claude.exe"]


class _Done:
    returncode, stdout, stderr = 0, "ok", ""


def _capture_run(monkeypatch, launcher):
    seen = {}
    monkeypatch.setattr(tools, "_direct_launcher", lambda b: list(launcher))
    monkeypatch.setattr(tools, "_audit_log", lambda *a, **k: None)
    monkeypatch.setattr(tools.subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd, shell=kw.get("shell", False)) or _Done())
    return seen


def test_opencli_runs_without_a_shell_and_keeps_quoted_text_whole(monkeypatch):
    seen = _capture_run(monkeypatch, ["node", "main.js"])
    command = 'hackernews search "web scraping & more"'
    tools.run_opencli(command)                  # asks first
    tools.run_opencli(command, confirm=True)
    assert seen == {"cmd": ["node", "main.js", "hackernews", "search", "web scraping & more"],
                    "shell": False}


def test_web_browse_never_uses_a_shell(monkeypatch):
    seen = _capture_run(monkeypatch, ["node", "oc.js"])
    tools.web_browse("https://example.com/a&b")
    assert seen["shell"] is False and seen["cmd"] == ["node", "oc.js", "open", "https://example.com/a&b",
                                                      "--budget", "500"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_multi_line_task_arrives_whole(tmp_path, monkeypatch):
    js = "node_modules/@google/gemini-cli/bundle/echo.js"
    _shim(tmp_path, monkeypatch, NODE_SHIM.format(js="echo.js"), js)
    (tmp_path / js).write_text("process.stdout.write(JSON.stringify(process.argv.slice(2)))")
    task = 'CONTEXT for CODE delegate:\n[PROFILE]\nfix the "chat" & echo hacked | more'
    out = subprocess.run(tools._direct_launcher("agent") + [task], capture_output=True,
                         text=True, encoding="utf-8").stdout
    import json
    assert json.loads(out) == [task]
