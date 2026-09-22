"""Three bugs a model switch surfaced (prod check, 2026-09-22):
- a tool that returns a list (recall_facts) crashed the agent loop's model
  attempt ("'list' object has no attribute 'startswith'");
- printing a tool result the Windows console cannot encode ended the turn;
- a relative path ("Desktop") passed the folder check as the user's Desktop
  but was read relative to the server's folder, so the model was shown
  Cygnus's own desktop/ app folder."""

import io
import json
import sys
from types import SimpleNamespace as NS

import pytest

from cygnus import tools
from cygnus.brain import agent_loop as al


# ------------------------------------------------------------ agent loop --

def _reply(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


def _client(script):
    it = iter(script)
    return NS(chat=NS(completions=NS(create=lambda **kw: next(it))))


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.setattr(al, "_models", lambda: ["fake"])
    monkeypatch.setattr(al, "_system", lambda ctx: "system")

    def install(result, script):
        monkeypatch.setattr(al, "_run_tool", lambda n, a, c: result)
        monkeypatch.setattr(al, "_make_client", lambda: _client(script))
    return install


def _brain():
    return NS(conversation=[{"role": "user", "content": "the turn"}])


def test_a_tool_that_returns_a_list_does_not_crash_the_turn(loop):
    call = NS(id="c1", function=NS(name="recall", arguments=json.dumps({"query": "me"})))
    loop([{"id": 20, "text": "User runs a musubi business."}],
         [_reply(tool_calls=[call]), _reply("You run a musubi business, sir.")])
    assert al.run(_brain(), "what do you know about me", {"client": "l"}) == "You run a musubi business, sir."
    assert al.last_stats()["broke"] is None


def test_a_result_the_console_cannot_print_does_not_end_the_turn(loop, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    call = NS(id="c1", function=NS(name="read_window", arguments=json.dumps({"app": "hermes"})))
    loop("Hermes shows: Smart. ￼ v0.21", [_reply(tool_calls=[call]), _reply("Hermes is idle, sir.")])
    assert al.run(_brain(), "what does hermes show", {"client": "p"}) == "Hermes is idle, sir."


# ------------------------------------------------------- relative paths --

@pytest.fixture
def two_desktops(tmp_path, monkeypatch):
    """The user's Desktop, and a same-named folder where the server runs."""
    home_desktop = tmp_path / "home" / "Desktop"
    home_desktop.mkdir(parents=True)
    (home_desktop / "users-notes.txt").write_text("the user's file")
    server = tmp_path / "server"
    (server / "Desktop").mkdir(parents=True)
    (server / "Desktop" / "main.js").write_text("cygnus internals")
    monkeypatch.chdir(server)
    monkeypatch.setattr(tools, "_resolve_write_path", lambda p: tmp_path / "home" / p)
    monkeypatch.setattr(tools, "_is_allowed_path", lambda p: str(p).startswith(str(tmp_path / "home")))
    return home_desktop


def test_list_directory_lists_the_users_folder(two_desktops):
    out = tools.list_directory("Desktop")
    assert "users-notes.txt" in out and "main.js" not in out


def test_read_file_reads_the_users_file(two_desktops):
    assert tools.read_file("Desktop/users-notes.txt").endswith("the user's file") or \
        "the user's file" in tools.read_file("Desktop/users-notes.txt")


def test_search_files_searches_the_users_folder(two_desktops):
    out = tools.search_files("internals", "Desktop")
    assert "main.js" not in out
