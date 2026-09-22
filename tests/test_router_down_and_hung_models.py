"""A turn with 9router down starts it and waits, and says so when it stays
down; a model that hangs hands over after FIRST_CALL_S; a connection error
does not walk the other models through the same dead router (2026-09-22)."""

import json
from types import SimpleNamespace as NS

import pytest

import agent_loop as al
import brain_gemini as bg
import router_keeper


def test_a_router_that_is_up_needs_nothing(monkeypatch):
    monkeypatch.setattr(router_keeper, "ensure_up", lambda wait=20: pytest.fail("not needed"))
    bg._router_ready()


def test_a_router_that_is_down_is_started_and_waited_for(monkeypatch):
    waits = []
    monkeypatch.setattr(router_keeper, "port_open", lambda timeout=0.5: False)
    monkeypatch.setattr(router_keeper, "ensure_up", lambda wait=20: waits.append(wait) or True)
    bg._router_ready()
    assert waits and 0 < waits[0] <= bg.ROUTER_START_WAIT_S


def test_a_router_that_stays_down_is_said_plainly(monkeypatch):
    monkeypatch.setattr(router_keeper, "port_open", lambda timeout=0.5: False)
    monkeypatch.setattr(router_keeper, "ensure_up", lambda wait=20: False)
    with pytest.raises(bg._RouterDown):
        bg._router_ready()


def reply(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


class Client:
    def __init__(self, by_model):
        self.by_model, self.asked = by_model, []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, model, timeout=None, **kw):
        self.asked.append((model, timeout))
        out = self.by_model[model]
        if isinstance(out, Exception):
            raise out
        return out


class Brain:
    def __init__(self):
        self.conversation = [{"role": "user", "content": "the turn"}]


@pytest.fixture
def loop(monkeypatch):
    monkeypatch.setattr(al, "_system", lambda ctx: "system")
    monkeypatch.setattr(bg, "_report_unhealthy", lambda m, r: None)

    def install(models, by_model):
        client = Client(by_model)
        monkeypatch.setattr(al, "_models", lambda: models)
        monkeypatch.setattr(al, "_make_client", lambda: client)
        return client
    return install


def test_a_hung_first_model_hands_over_after_the_first_call_cap(loop):
    client = loop(["ag/claude-sonnet-4-6", "ag/gemini-3-flash"],
                  {"ag/claude-sonnet-4-6": TimeoutError("Request timed out."),
                   "ag/gemini-3-flash": reply("Here you go, sir.")})
    assert al.run(Brain(), "tell me something", {"client": "h"}) == "Here you go, sir."
    assert client.asked[0] == ("ag/claude-sonnet-4-6", bg.FIRST_CALL_S)


def test_a_connection_error_stops_the_walk(loop):
    import openai
    err = openai.APIConnectionError(request=NS(method="POST", url="http://localhost:20128/v1"))
    client = loop(["ag/claude-sonnet-4-6", "ag/gemini-3-flash"],
                  {"ag/claude-sonnet-4-6": err, "ag/gemini-3-flash": reply("unreached")})
    assert al.run(Brain(), "tell me something", {"client": "c"}) is None   # old routing takes it
    assert [m for m, _ in client.asked] == ["ag/claude-sonnet-4-6"]


def test_the_loop_hands_back_when_the_router_stays_down(loop, monkeypatch):
    client = loop(["ag/claude-sonnet-4-6"], {"ag/claude-sonnet-4-6": reply("unreached")})
    monkeypatch.setattr(router_keeper, "port_open", lambda timeout=0.5: False)
    monkeypatch.setattr(router_keeper, "ensure_up", lambda wait=20: False)
    assert al.run(Brain(), "tell me something", {"client": "d"}) is None
    assert client.asked == []
    assert al._lock.acquire(blocking=False)                          # the lock was released
    al._lock.release()
