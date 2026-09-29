"""The Cua driver as Cygnus's hands inside a window: a chat, tab or list
entry opened by the name the owner said. The driver is faked; no test
touches a real window. Window text (Telegram's messages are list items too)
is never offered to the model, reported, or matched against."""

import json

import pytest

from cygnus.apps import commands, cua
from cygnus import safety

MESSAGE = ("Chloe\n"
           "Hey, the EUR/USD backtest finished overnight\n"
           "win rate 41%, drawdown 12%\n"
           "let's talk cerebrum tomorrow\n"
           "21:04")


def _el(i, role, label, actions=("invoke", "select"), enabled=True, parent=None, title=None):
    return {"element_index": i, "role": role, "label": label, "actions": list(actions),
            "enabled": enabled, "element_token": f"tok-{i}", "parent_index": parent,
            "_title": title}


# Telegram as measured on 2026-09-25: a "Chats" list of one-line items with
# comma-separated fields (a name, then the last message's preview and time),
# and a "Messages" list. Opening a chat titles the window with its name.
TELEGRAM = [
    _el(1, "List", "Chats", actions=()),
    _el(2, "ListItem", "cerebrum (eur/usd), You: sent the chart, 21:04", parent=1, title="cerebrum (eur/usd)"),
    _el(3, "ListItem", "Musubae team, Chloe: backtest photo, 20:11", parent=1, title="Musubae team"),
    _el(4, "ListItem", "3 Saved Messages", parent=1, title="Saved Messages"),
    _el(5, "ListItem", "Pinned, Ops desk, Ana: rota is up, 09:00", parent=1, title="Ops desk"),
    _el(10, "List", "Messages", actions=()),
    _el(11, "ListItem", MESSAGE, parent=10),                        # a message, not a chat
    _el(12, "ListItem", "Saved Messages\nforwarded", parent=10),    # message text that looks like a name
    _el(20, "DataItem", "21:04", actions=("set_value",)),
    _el(21, "Button", "Send"),
]
TERMINAL_A = [_el(1, "Tab", "", actions=()),
              _el(2, "TabItem", "◑ CYGNUS prompt delegation to AI agents", actions=("select",), parent=1)]
TERMINAL_B = [_el(1, "Tab", "", actions=()),
              _el(2, "TabItem", "✳ Vault and musubae business exploration", actions=("select",), parent=1)]


class FakeDriver:
    """cua.call, answering per window, and the window titles Windows would
    show; a click retitles the window the way Telegram and Terminal do."""

    def __init__(self, windows):
        self.windows = windows          # {hwnd: [elements]}
        self.titles = {h: "before" for h in windows}
        self.calls = []

    def __call__(self, tool, args, timeout=None):
        self.calls.append((tool, args))
        if tool == "get_window_state":
            els = [{k: v for k, v in e.items() if k != "_title"} for e in self.windows[args["window_id"]]]
            return {"snapshot_id": "s1", "elements": els}
        if tool == "click":
            el = next(e for e in self.windows[args["window_id"]] if e["element_token"] == args["element_token"])
            self.titles[args["window_id"]] = el.get("_title") or el["label"]
            return {"delivery": {"mode": "background"}}
        if tool == "bring_to_front":
            return {}
        raise AssertionError(f"unexpected tool {tool}")

    def clicked(self):
        return [a for t, a in self.calls if t == "click"]


@pytest.fixture
def driver(monkeypatch):
    monkeypatch.setattr(cua, "VERIFY_WAIT_S", 0)

    def make(windows):
        fake = FakeDriver(windows)
        monkeypatch.setattr(cua, "call", fake)
        monkeypatch.setattr(cua, "window_pid", lambda hwnd: 1000 + int(hwnd))
        monkeypatch.setattr(cua, "window_title", lambda hwnd: cua.item_text(fake.titles[hwnd]))
        return fake
    return make


def _row(name, *hwnds):
    return {"id": f"row-{name}", "name": name, "group": "apps", "pids": [1],
            "windows": [{"hwnd": h, "title": f"{name} {h}"} for h in hwnds]}


# ------------------------------------------------------------------ names --

CHATS = ["cerebrum (eur/usd), You: sent the chart, 21:04",
         "Musubae team, Chloe: backtest photo, 20:11",
         "3 Saved Messages",
         "Pinned, Ops desk, Ana: rota is up, 09:00"]


@pytest.mark.parametrize("said, want", [
    ("cerebrum", ([0], False)),
    ("the cerebrum chat", ([0], False)),
    ("cerebrum eur usd", ([0], True)),
    ("musubae team", ([1], True)),
    ("saved messages", ([2], True)),         # past the unread count, every word: exact
    ("ops desk", ([3], True)),               # the name in the second field
    ("backtest", ([], False)),               # only in a preview
    ("chart", ([], False)),
    ("rota", ([], False)),                   # a third field is always preview
    ("the chat", ([], False)),               # names nothing
])
def test_a_list_item_is_matched_by_a_name_field_never_its_preview(said, want):
    assert cua.match(said, CHATS) == want


def test_an_exact_name_wins_over_a_longer_one():
    assert cua.match("cerebrum", ["cerebrum (eur/usd), hi", "Cerebrum, hello"]) == ([1], True)


def test_a_first_field_match_wins_over_a_second_field_one():
    # "cerebrum (eur/usd)" posting in another chat puts its name second there.
    assert cua.match("cerebrum", ["Traders, cerebrum (eur/usd), chart", "cerebrum (eur/usd), hi"]) == ([1], False)


def test_a_tab_title_matches_on_any_of_its_words():
    tabs = ["◑ CYGNUS prompt delegation to AI agents", "✳ Vault and musubae business exploration"]
    assert cua.match("delegation", tabs, [True, True]) == ([0], False)
    assert cua.match("delegation", tabs) == ([], False)          # as list items it would not


def test_an_items_text_is_its_first_line_without_invisible_marks():
    assert cua.item_text("‎cerebrum (eur/usd)\nsecond line") == "cerebrum (eur/usd)"
    assert cua.item_text("") == ""


def test_a_list_item_is_called_what_the_owner_called_it():
    assert cua.shown_name("cerebrum", "ListItem", CHATS[0]) == "cerebrum"
    assert cua.shown_name("vault", "TabItem", "Vault and musubae") == "Vault and musubae"


# ------------------------------------------------------------ open_item --

def test_it_opens_the_chat_checks_the_title_and_brings_the_window_forward(driver):
    fake = driver({77: TELEGRAM})
    out = cua.CuaWindow(_row("Telegram", 77)).open_item("cerebrum")
    assert out == "Opened “cerebrum” in Telegram."           # no preview said back
    tools = [(t, a.get("element_token")) for t, a in fake.calls]
    assert tools == [("get_window_state", None), ("click", "tok-2"), ("bring_to_front", None)]
    assert fake.calls[1][1]["pid"] == 1077 and fake.calls[1][1]["window_id"] == 77


def test_a_click_the_title_does_not_confirm_is_not_claimed(driver):
    fake = driver({77: TELEGRAM})
    fake.windows[77][1]["_title"] = "Something else"               # the app opened another chat
    out = cua.CuaWindow(_row("Telegram", 77)).open_item("cerebrum")
    assert out.startswith("I selected something in Telegram for “cerebrum”")
    assert "may not be the one you meant" in out and "Opened" not in out


def test_no_screenshot_is_ever_asked_for(driver):
    fake = driver({77: TELEGRAM})
    cua.CuaWindow(_row("Telegram", 77)).open_item("saved messages")
    (state,) = [a for t, a in fake.calls if t == "get_window_state"]
    assert state["include_screenshot"] is False


def test_a_message_is_never_matched_or_read_out(driver):
    fake = driver({77: TELEGRAM})
    for said in ("backtest", "chloe", "win rate", "rota"):
        with pytest.raises(commands.DriveError) as e:
            cua.CuaWindow(_row("Telegram", 77)).open_item(said)
        assert "backtest finished" not in str(e.value) and "Musubae" not in str(e.value)
    assert fake.clicked() == []


def test_an_item_in_the_messages_list_is_not_a_place_to_go(driver):
    fake = driver({77: TELEGRAM})
    cua.CuaWindow(_row("Telegram", 77)).open_item("saved messages")
    (click,) = fake.clicked()
    assert click["element_token"] == "tok-4"                        # the chat, not the message


def test_two_chats_that_match_are_counted_not_read_out(driver):
    fake = driver({77: TELEGRAM + [_el(6, "ListItem", "Musubae orders, Ana: 12 new orders", parent=1)]})
    with pytest.raises(commands.DriveError) as e:
        cua.CuaWindow(_row("Telegram", 77)).open_item("musubae")
    assert str(e.value) == "2 entries in Telegram match “musubae”. Say more of the name"
    assert fake.clicked() == []


def test_two_tabs_that_match_are_named(driver):
    driver({11: TERMINAL_A + [_el(3, "TabItem", "CYGNUS tests", actions=("select",), parent=1)]})
    with pytest.raises(commands.DriveError) as e:
        cua.CuaWindow(_row("Windows Terminal", 11)).open_item("cygnus")
    assert "CYGNUS tests" in str(e.value) and "delegation" in str(e.value)


def test_a_tab_is_found_in_whichever_window_holds_it(driver):
    fake = driver({11: TERMINAL_A, 22: TERMINAL_B})
    out = cua.CuaWindow(_row("Windows Terminal", 11, 22)).open_item("the vault tab")
    assert out == "Opened the tab “✳ Vault and musubae business exploration” in Windows Terminal."
    (click,) = fake.clicked()
    assert click["window_id"] == 22


def test_buttons_and_disabled_entries_are_not_items(driver):
    driver({77: [_el(1, "List", "Chats", actions=()), _el(2, "Button", "Send"),
                 _el(3, "ListItem", "Send", enabled=False, parent=1)]})
    with pytest.raises(commands.DriveError):
        cua.CuaWindow(_row("Telegram", 77)).open_item("send")


def test_a_driver_failure_is_a_drive_error(monkeypatch):
    def broken(tool, args, timeout=None):
        raise cua.CuaError("the Cua driver's get_window_state failed: pid gone")
    monkeypatch.setattr(cua, "call", broken)
    monkeypatch.setattr(cua, "window_pid", lambda hwnd: 1)
    with pytest.raises(commands.DriveError, match="pid gone"):
        cua.CuaWindow(_row("Telegram", 77)).open_item("cerebrum")


# ---------------------------------------------------------------- the CLI --

def test_no_driver_installed_means_not_available(monkeypatch):
    monkeypatch.setattr(cua, "exe", lambda: None)
    monkeypatch.setitem(cua._status, "at", 0.0)
    assert cua._daemon_up() is False


def test_the_daemon_check_is_cached(monkeypatch):
    runs = []

    class Proc:
        stdout = "Cua Driver daemon is running"
        stderr = ""
    monkeypatch.setattr(cua, "exe", lambda: "cua-driver.exe")
    monkeypatch.setattr(cua, "_run", lambda args, timeout=None: runs.append(args) or Proc())
    monkeypatch.setitem(cua._status, "at", 0.0)
    assert cua._daemon_up() and cua._daemon_up()
    assert runs == [["status"]]


def test_an_error_answer_raises(monkeypatch):
    class Proc:
        stdout = json.dumps({"isError": True, "error": "window_id does not belong to pid"})
        stderr = ""
    monkeypatch.setattr(cua, "_run", lambda args, timeout=None: Proc())
    with pytest.raises(cua.CuaError, match="does not belong"):
        cua.call("click", {"pid": 1, "window_id": 2})


def test_a_non_json_answer_raises(monkeypatch):
    class Proc:
        stdout = "daemon not running"
        stderr = ""
    monkeypatch.setattr(cua, "_run", lambda args, timeout=None: Proc())
    with pytest.raises(cua.CuaError, match="daemon not running"):
        cua.call("list_windows", {})


# -------------------------------------------------------------- commands --

@pytest.fixture
def cua_on(monkeypatch):
    monkeypatch.setattr(cua, "available", lambda: True)


def test_an_app_with_nothing_else_gets_the_cua_driver(cua_on):
    row = _row("Telegram", 77)
    (t,) = commands.targets([row], {})
    assert t["driver"] == "cua"
    assert [a["do"] for a in commands.actions(t, row, {})] == ["open_item"]


def test_without_the_driver_nothing_changes():
    row = _row("Telegram", 77)
    (t,) = commands.targets([row], {})
    assert t["driver"] is None


def test_hermes_keeps_its_own_actions(cua_on):
    row = dict(_row("Hermes desktop app", 5), group="agents")
    (t,) = commands.targets([row], {})
    assert t["driver"] == "hermes-window"
    assert "open_item" not in [a["do"] for a in commands.actions(t, row, {})]


def test_the_model_is_never_shown_the_windows_names(cua_on):
    row = _row("Telegram", 77)
    (t,) = commands.targets([row], {})
    (act,) = commands.actions(t, row, {})
    assert "choices" not in act


def test_an_open_item_step_runs_through_the_driver(cua_on, driver):
    driver({77: TELEGRAM})
    row = _row("Telegram", 77)
    step = commands._step_from({"do": "open_item", "args": {"name": "saved messages"}},
                               {"open_item": commands.OPEN_ITEM})
    assert step == {"op": "open_item", "name": "saved messages"}
    out = commands._run("telegram", [step], rows=[row], apps={})
    assert out == "Opened “saved messages” in Telegram."


def test_a_miss_stops_and_says_so(cua_on, driver):
    driver({77: TELEGRAM})
    out = commands._run("telegram", [{"op": "open_item", "name": "accounting"}],
                        rows=[_row("Telegram", 77)], apps={})
    assert out == "I stopped: I couldn't find anything called “accounting” in Telegram."


@pytest.mark.parametrize("said, name", [
    ("open cerebrum", "cerebrum"),
    ("switch to the vault tab", "vault"),
    ("go to the musubae team chat", "musubae team"),
    ("tell them I'm late", None),
])
def test_with_no_model_the_open_phrasing_still_reads(said, name):
    assert commands.open_item_phrase(said) == name


def test_with_no_model_an_open_is_still_understood(cua_on, monkeypatch):
    monkeypatch.setattr(commands, "_ask_model", lambda prompt: (None, "down"))
    got = commands.understand("telegram", "open cerebrum", rows=[_row("Telegram", 77)], apps={})
    assert got["steps"] == [{"op": "open_item", "name": "cerebrum"}]


# ---------------------------------------------------------------- safety --

def test_opening_a_terminal_tab_needs_no_yes():
    assert safety.assess_app_step("terminal", {"do": "open_item", "name": "vault"}) is safety.ACT


def test_typing_in_a_terminal_still_asks():
    d = safety.assess_app_step("terminal", {"do": "ability:wt:type_x", "label": "Type"})
    assert d is not safety.ACT


@pytest.mark.parametrize("printed, said", [
    ("The operation completed successfully. (0x00000000)", "Windows refused the input for that window"),
    ("foreground_unavailable: Windows did not activate exact target HWND 0x1802a2",
     "Windows would not bring that window forward, so nothing was clicked"),
])
def test_a_failed_click_that_exits_zero_is_said_plainly(monkeypatch, printed, said):
    class Proc:
        stdout = printed
        stderr = ""
    monkeypatch.setattr(cua, "_run", lambda args, timeout=None: Proc())
    with pytest.raises(cua.CuaError) as e:
        cua.call("click", {"pid": 1, "window_id": 2, "x": 3, "y": 4})
    assert str(e.value) == f"the Cua driver's click failed: {said}"
