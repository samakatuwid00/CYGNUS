"""router_keeper: 9router is started when its port is shut, once, and a
router that is already starting is waited for instead of started twice."""

import pytest

import router_keeper as rk


@pytest.fixture
def keeper(monkeypatch):
    k = {"open": False, "proc": False, "spawned": [], "open_after_spawn": True}
    monkeypatch.setattr(rk, "ENABLED", True)
    monkeypatch.setattr(rk, "_last_start", 0.0)
    monkeypatch.setattr(rk, "port_open", lambda timeout=0.5: k["open"])
    monkeypatch.setattr(rk, "_router_process_running", lambda: k["proc"])
    monkeypatch.setattr(rk, "_command", lambda: ["9router", "--tray"])

    def spawn(cmd):
        k["spawned"].append(cmd)
        if k["open_after_spawn"]:
            k["open"] = True
    monkeypatch.setattr(rk, "_spawn", spawn)
    monkeypatch.setattr(rk.time, "sleep", lambda s: None)
    return k


def test_an_open_port_starts_nothing(keeper):
    keeper["open"] = True
    assert rk.ensure_up() is True
    assert keeper["spawned"] == []


def test_a_shut_port_starts_9router_and_waits_for_it(keeper):
    assert rk.ensure_up(wait=1) is True
    assert keeper["spawned"] == [["9router", "--tray"]]
    assert rk.state["up"] is True


def test_a_router_already_starting_is_waited_for_not_started_again(keeper):
    keeper["proc"] = True                 # the login script's router, still booting
    assert rk.ensure_up(wait=0) is False
    assert keeper["spawned"] == []


def test_a_router_that_will_not_come_up_is_not_restarted_every_turn(keeper):
    keeper["open_after_spawn"] = False
    assert rk.ensure_up(wait=0) is False
    assert rk.ensure_up(wait=0) is False
    assert len(keeper["spawned"]) == 1    # the next start waits RESTART_GAP_S


def test_off_it_only_reports(keeper, monkeypatch):
    monkeypatch.setattr(rk, "ENABLED", False)
    assert rk.ensure_up() is False
    assert keeper["spawned"] == []


@pytest.mark.parametrize("err, down", [
    ("APIConnectionError: Connection error.", True),
    ("[WinError 10061] No connection could be made because the target machine actively refused it", True),
    ("Error code: 503 - [codex/gpt-5.5] [429]: The usage limit has been reached", False),
    ("Request timed out.", False)])
def test_what_counts_as_the_router_being_down(err, down):
    assert rk.is_connection_error(err) is down
