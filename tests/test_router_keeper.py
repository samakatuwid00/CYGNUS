"""router_keeper: 9router is started when its port is shut, once, and a
router that is already starting is waited for instead of started twice."""

import pytest

from cygnus.brain import router_keeper as rk


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


# ---- a router whose port is open but which answers nothing (2026-09-22) ----

@pytest.fixture
def hung(keeper, monkeypatch):
    keeper["open"] = True
    keeper.update(responds=False, stopped=[])
    monkeypatch.setattr(rk, "_misses", 0)
    monkeypatch.setattr(rk, "state", {"up": None, "started": 0, "restarts_hung": 0, "last_error": ""})
    monkeypatch.setattr(rk, "responds", lambda timeout=5.0: keeper["responds"])

    def stop():
        keeper["stopped"].append(True)
        keeper["open"] = False
        keeper["responds"] = True           # the fresh one will answer
        return 2
    monkeypatch.setattr(rk, "_stop_router", stop)
    return keeper


def test_one_slow_answer_is_not_a_hang(hung):
    assert rk.check() == "slow"
    assert hung["stopped"] == [] and hung["spawned"] == []


def test_a_router_that_answers_nothing_three_times_is_restarted(hung):
    rk.check()
    assert rk.check() == "slow"             # two misses: a busy machine, not a hang
    assert rk.check() == "restarted"
    assert hung["stopped"] == [True] and len(hung["spawned"]) == 1
    assert rk.state["restarts_hung"] == 1


def test_an_answer_clears_the_count(hung):
    rk.check()
    hung["responds"] = True
    assert rk.check() == "ok"
    hung["responds"] = False
    assert rk.check() == "slow"             # counting starts again
    assert hung["stopped"] == []


def test_a_shut_port_is_started_by_check(keeper):
    assert rk.check() == "started"
    assert len(keeper["spawned"]) == 1


# ---- someone else's machine: an endpoint Cygnus does not run (BYO key) ----

def test_a_remote_endpoint_is_never_started_or_stopped(keeper, monkeypatch):
    monkeypatch.setattr(rk, "_address", lambda: ("api.openai.com", 443))
    assert rk.is_local() is False
    assert rk.ensure_up(wait=0) is False          # not reachable in this fake
    assert keeper["spawned"] == []
    keeper["open"] = True
    assert rk.ensure_up() is True                 # reachable: nothing to do
    assert keeper["spawned"] == []


def test_a_remote_endpoint_that_goes_quiet_is_not_restarted(hung, monkeypatch):
    monkeypatch.setattr(rk, "_address", lambda: ("openrouter.ai", 443))
    for _ in range(rk.HUNG_AFTER + 1):
        assert rk.check() in ("slow", "ok")
    assert hung["stopped"] == [] and hung["spawned"] == []


@pytest.mark.parametrize("host, local", [
    ("localhost", True), ("127.0.0.1", True), ("::1", True), ("0.0.0.0", True),
    ("api.openai.com", False), ("openrouter.ai", False), ("192.168.1.9", False)])
def test_which_endpoints_cygnus_runs_itself(host, local, monkeypatch):
    monkeypatch.setattr(rk, "_address", lambda: (host, 20128))
    assert rk.is_local() is local
