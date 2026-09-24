"""A held question belongs to the device it was asked on.

Found by scripts/bench/concurrency.py on 2026-09-23: two HUDs, one server. The
phone was asked "delete the file ...?"; a "yes" typed on the desktop released
it ("confirmed - launching Hermes" in the job's own progress). Cygnus keeps a
conversation per client, but every confirm latch was a single global.
"""

import pytest

from cygnus import safety
from cygnus import tools


@pytest.fixture
def as_client(monkeypatch):
    """Speak as a named HUD."""
    def use(name):
        monkeypatch.setattr(safety, "current_client", lambda: name)
        monkeypatch.setattr(tools, "_current_client", lambda: name)
    return use


@pytest.fixture(autouse=True)
def clean_latches(monkeypatch):
    monkeypatch.setattr(safety, "_pending", None, raising=False)
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL", None, raising=False)
    yield
    safety._pending = None
    tools._PENDING_HERMES_CALL = None


def _hold_a_delete():
    ran = []
    decision = safety.Decision("confirm", "it deletes a file", "Delete it, sir?")
    safety.hold("delete_file {}", decision, lambda: ran.append(True) or "Deleted, sir.")
    return ran


def test_another_device_cannot_confirm_what_it_was_not_asked(as_client):
    as_client("phone")
    ran = _hold_a_delete()
    safety.begin_turn()             # a yes only counts in a later turn

    as_client("desktop")
    assert safety.confirm_pending() is None
    assert not ran, "the desktop released the phone's delete"
    assert safety.pending(), "the question must stay held for the phone"


def test_the_device_that_was_asked_still_confirms(as_client):
    as_client("phone")
    ran = _hold_a_delete()
    safety.begin_turn()

    as_client("desktop")
    safety.confirm_pending()            # stray yes, ignored
    as_client("phone")
    assert safety.confirm_pending() == "Deleted, sir."
    assert ran == [True]


def test_another_device_cannot_decline_it_either(as_client):
    as_client("phone")
    _hold_a_delete()
    safety.begin_turn()

    as_client("desktop")
    assert safety.decline_pending() is None
    assert safety.pending(), "the desktop's no dropped the phone's question"

    as_client("phone")
    assert safety.decline_pending()
    assert safety.pending() is None


def test_held_elsewhere_names_the_question(as_client):
    as_client("phone")
    _hold_a_delete()
    as_client("desktop")
    assert safety.held_elsewhere() == "Delete it, sir?"
    as_client("phone")
    assert safety.held_elsewhere() is None


def test_a_task_latch_is_not_this_devices_to_confirm(as_client, monkeypatch):
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL",
                        {"task": "delete old.txt", "raw_task": "delete old.txt",
                         "ts": __import__("time").time(), "client": "phone"},
                        raising=False)
    as_client("desktop")
    assert tools.pending_confirm_task() is None
    assert tools.pending_confirm_elsewhere() == "delete old.txt"
    assert tools.confirm_pending() is None
    assert tools.decline_pending() is None
    assert tools._PENDING_HERMES_CALL, "the desktop dropped the phone's task"

    as_client("phone")
    assert tools.pending_confirm_task() == "delete old.txt"
    assert tools.pending_confirm_elsewhere() is None


def test_a_latch_with_no_client_still_works(as_client, monkeypatch):
    """Older latches, and turns with no client, keep the default behaviour."""
    monkeypatch.setattr(tools, "_PENDING_HERMES_CALL",
                        {"task": "t", "raw_task": "t", "ts": __import__("time").time()},
                        raising=False)
    as_client("default")
    assert tools.pending_confirm_task() == "t"


# --- the regression: the fix above blocked the owner in their own window ----

def test_the_same_window_can_answer_a_latch_made_on_a_worker_thread(as_client):
    """2026-09-23, from the owner's own session log:

        11:32:41  you     proceed with 1
        11:32:41  Cygnus  [NEEDS_CONFIRM] ... Say 'confirm'
        11:32:47  you     confirm
        11:32:47  Cygnus  That confirm was asked on another device, sir

    One HUD, one window. The latch was created off the request thread, where
    the thread-local client is empty, so it recorded "default" - and "default"
    was compared as if it were a different device.
    """
    import threading

    made = {}

    def worker():                      # a tool running on a background thread
        made["client"] = safety.current_client()
        _hold_a_delete()
    as_client("browser-abc")
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    safety.begin_turn()

    held = safety.pending()
    assert held, "nothing was held"
    assert safety.held_elsewhere() is None, "the owner's own window was told to go elsewhere"
    assert safety.confirm_pending() == "Deleted, sir."


def test_an_unknown_client_can_still_answer(as_client):
    """A turn with no client (the CLI, a script) must not be locked out."""
    as_client("phone")
    ran = _hold_a_delete()
    safety.begin_turn()
    as_client("default")
    assert safety.confirm_pending() == "Deleted, sir."
    assert ran == [True]


def test_two_known_devices_are_still_kept_apart(as_client):
    """The protection that started all this must survive the fix."""
    as_client("phone")
    ran = _hold_a_delete()
    safety.begin_turn()
    as_client("desktop")
    assert safety.confirm_pending() is None
    assert not ran
    assert safety.held_elsewhere() == "Delete it, sir?"
