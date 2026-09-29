"""Layer 2 of seeing: a window's words read by OCR on this PC, for apps that
describe nothing to UI Automation. The screen, the OCR engine and the Cua
driver are faked, except in the one test that reads a rendered image with
the real engine when it is installed."""

import pytest

from cygnus.apps import commands, cua, screen_text
from cygnus import safety


def _line(text, x=10, y=10, word_w=40, gap=5, h=16):
    words, cx = [], x
    for w in text.split():
        words.append({"text": w, "x": cx, "y": y, "w": word_w, "h": h})
        cx += word_w + gap
    return {"text": text, "words": words}


BRAVE_MENU = [_line("YouTube Music", y=20), _line("Home", y=190), _line("Explore", y=245),
              _line("Library", y=298), _line("Downloads", y=350),
              _line("Your downloads will appear here", y=600)]


# ------------------------------------------------------------------ find --

@pytest.mark.parametrize("said, want", [
    ("downloads", [("Downloads", True)]),              # the menu entry, not the sentence
    ("the explore tab", [("Explore", True)]),
    ("will appear", [("will appear", False)]),
    ("appear will", []),                               # words must be in order
    ("settings", []),
    ("the", []),
])
def test_what_the_spoken_words_find(said, want):
    assert [(h["text"], h["exact"]) for h in screen_text.find(BRAVE_MENU, said)] == want


def test_a_hit_is_the_centre_of_its_words():
    (hit,) = screen_text.find([_line("Save as copy", x=100, y=50)], "save as")
    assert (hit["x"], hit["y"]) == (100 + (40 + 5 + 40) // 2, 58)


def test_punctuation_and_case_do_not_matter():
    assert screen_text.find([_line("Sign-in:", y=5)], "signin")[0]["text"] == "Sign-in:"


def test_a_changed_window_is_told_from_an_unchanged_one():
    after = BRAVE_MENU[:-1] + [_line("No downloads yet"), _line("Clear all")]
    assert screen_text.changed(BRAVE_MENU, after)
    assert not screen_text.changed(BRAVE_MENU, list(BRAVE_MENU))


# ------------------------------------------------------------- click_text --

class Screen:
    """What the window shows, read by read(); a click can change it."""

    def __init__(self, before, after=None):
        self.before, self.after, self.clicked = before, after if after is not None else before, False
        self.calls = []

    def read(self, pid, hwnd):
        return self.after if self.clicked else self.before

    def call(self, tool, args, timeout=None):
        self.calls.append((tool, args))
        if tool == "click":
            self.clicked = True
            return {}
        if tool == "bring_to_front":
            return {}
        if tool == "get_window_state":
            return {"elements": []}                  # an app that describes nothing
        raise AssertionError(tool)


@pytest.fixture
def screen(monkeypatch):
    monkeypatch.setattr(cua, "CLICK_SETTLE_S", 0)
    monkeypatch.setattr(cua, "VERIFY_WAIT_S", 0)
    monkeypatch.setattr(cua, "window_pid", lambda hwnd: 500)

    def make(before, after=None, ocr=True):
        s = Screen(before, after)
        monkeypatch.setattr(cua, "call", s.call)
        monkeypatch.setattr(screen_text, "read", s.read)
        monkeypatch.setattr(screen_text, "available", lambda: ocr)
        return s
    return make


def _row(name, *hwnds):
    return {"id": f"row-{name}", "name": name, "group": "apps", "pids": [1],
            "windows": [{"hwnd": h, "title": name} for h in hwnds]}


def test_an_app_that_lists_nothing_is_clicked_where_the_words_are(screen):
    s = screen(BRAVE_MENU, BRAVE_MENU[:-1] + [_line("No downloads yet"), _line("Clear all")])
    out = cua.CuaWindow(_row("Brave", 9)).open_item("downloads")
    assert out == "Clicked “Downloads” in Brave, and the window changed."
    (click,) = [a for t, a in s.calls if t == "click"]
    assert (click["x"], click["y"]) == (30, 358) and click["window_id"] == 9
    assert "delivery_mode" not in click                    # background first


def test_a_click_that_changes_nothing_is_not_claimed(screen):
    screen(BRAVE_MENU)
    out = cua.CuaWindow(_row("Brave", 9)).open_item("library")
    assert out == ("I clicked “Library” in Brave, but nothing on its window changed, "
                   "so it may not have worked.")


def test_words_shown_twice_are_counted_not_guessed(screen):
    s = screen(BRAVE_MENU + [_line("Home", y=700)])
    with pytest.raises(commands.DriveError, match="appears 2 times"):
        cua.CuaWindow(_row("Brave", 9)).open_item("home")
    assert not [t for t, _ in s.calls if t == "click"]


def test_words_not_on_the_window_say_so(screen):
    screen(BRAVE_MENU)
    with pytest.raises(commands.DriveError, match="neither in its lists nor in the text on its window"):
        cua.CuaWindow(_row("Brave", 9)).open_item("settings")


def test_without_ocr_it_is_the_old_answer(screen):
    screen(BRAVE_MENU, ocr=False)
    with pytest.raises(commands.DriveError, match="couldn't find anything called"):
        cua.CuaWindow(_row("Brave", 9)).open_item("downloads")


def test_a_window_with_messages_is_never_read(screen, monkeypatch):
    s = screen(BRAVE_MENU)
    telegram = [{"element_index": 1, "role": "List", "label": "Messages", "actions": []},
                {"element_index": 2, "role": "ListItem", "label": "Downloads are ready", "actions": ["invoke"],
                 "element_token": "t2", "parent_index": 1, "enabled": True}]
    monkeypatch.setattr(cua, "call", lambda tool, args, timeout=None:
                        {"elements": telegram} if tool == "get_window_state" else s.call(tool, args))
    read = []
    monkeypatch.setattr(screen_text, "read", lambda pid, hwnd: read.append(1) or BRAVE_MENU)
    with pytest.raises(commands.DriveError, match="couldn't find anything called"):
        cua.CuaWindow(_row("Telegram", 7)).open_item("downloads")
    assert read == []                                      # no OCR of a chat window


def test_a_background_click_the_page_drops_is_repeated_in_front(monkeypatch):
    calls = []

    def fake(tool, args, timeout=None):
        calls.append(dict(args))
        if "delivery_mode" not in args:
            raise cua.CuaError("the Cua driver's click failed: background_unavailable (Chromium content)")
        return {}
    monkeypatch.setattr(cua, "call", fake)
    cua._click_at(1, 2, 30, 40)
    assert [c.get("delivery_mode") for c in calls] == [None, "foreground"]


def test_other_click_errors_are_not_retried(monkeypatch):
    def fake(tool, args, timeout=None):
        raise cua.CuaError("the Cua driver's click failed: pid 1 has no on-screen window")
    monkeypatch.setattr(cua, "call", fake)
    with pytest.raises(cua.CuaError, match="no on-screen window"):
        cua._click_at(1, 2, 30, 40)


# ------------------------------------------------------------------ safety --

@pytest.mark.parametrize("name", ["Delete", "send", "Pay now", "Sign out", "uninstall"])
def test_a_click_that_changes_something_asks_first(name):
    step = commands._step_from({"do": "open_item", "args": {"name": name}}, {"open_item": commands.OPEN_ITEM})
    assert step["ask"] is True                             # the "/" path: Run / Cancel
    assert safety.assess_app_step("brave", step) is not safety.ACT


@pytest.mark.parametrize("name", ["Downloads", "Explore", "cerebrum"])
def test_a_click_that_only_goes_somewhere_does_not(name):
    step = commands._step_from({"do": "open_item", "args": {"name": name}}, {"open_item": commands.OPEN_ITEM})
    assert "ask" not in step
    assert safety.assess_app_step("brave", step) is safety.ACT


# -------------------------------------------------------- the real engine --

def test_the_real_engine_reads_a_rendered_word(monkeypatch):
    pytest.importorskip("winrt.windows.media.ocr")
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    ImageFont = pytest.importorskip("PIL.ImageFont")
    import importlib
    import io
    real = importlib.import_module("cygnus.apps.screen_text")
    monkeypatch.undo()                                     # conftest's stub off, for the real check
    if not real.available():
        pytest.skip("no OCR language installed")
    img = Image.new("RGB", (420, 90), "white")
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except OSError:
        pytest.skip("no Arial to render with")
    ImageDraw.Draw(img).text((20, 20), "Downloads", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    lines = real.recognize(buf.getvalue())
    (hit,) = real.find(lines, "downloads")
    assert hit["exact"] and 20 < hit["x"] < 400 and 20 < hit["y"] < 80
