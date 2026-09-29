"""Reading a window's text on this PC, with Windows' own OCR engine.

Layer 2 of seeing (after the accessibility tree, before a vision model):
for apps that describe nothing to UI Automation (Brave's page and menus,
Settings, canvas apps), the window is captured by the Cua driver and its
words are read by Windows.Media.Ocr, which runs on the device. Neither the
image nor its text leaves the machine; what a caller reports is only the
words the owner said.

Coordinates are in the screenshot the Cua driver returned, which is the
space its click takes (x, y in "window-local screenshot pixels"), so a word
found here can be clicked there without any conversion.

Needs the winrt-Windows.Media.Ocr family of packages (requirements.txt).
Without them, or without an OCR language installed, available() is False
and callers carry on without this layer.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import re
import threading

_engine = {"checked": False, "ok": False}
_engine_lock = threading.Lock()


class OcrError(Exception):
    """The window could not be read; the message says why."""


def available() -> bool:
    """Windows OCR is installed and has a language for this user."""
    with _engine_lock:
        if not _engine["checked"]:
            try:
                from winrt.windows.media.ocr import OcrEngine
                _engine["ok"] = OcrEngine.try_create_from_user_profile_languages() is not None
            except Exception:
                _engine["ok"] = False
            _engine["checked"] = True
        return _engine["ok"]


async def _recognize_async(png: bytes):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise OcrError("Windows has no OCR language installed")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    writer.detach_stream()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    lines = []
    for line in result.lines:
        words = []
        for w in line.words:
            r = w.bounding_rect
            words.append({"text": w.text, "x": r.x, "y": r.y, "w": r.width, "h": r.height})
        lines.append({"text": line.text, "words": words})
    return lines


def recognize(png: bytes) -> list[dict]:
    """The text in an image: [{text, words: [{text, x, y, w, h}]}], one per line.
    Runs its own event loop, on another thread when this one already has one."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_recognize_async(png))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _recognize_async(png)).result()


def window_image(pid: int, hwnd: int) -> dict:
    """The window as the Cua driver sees it: {png, width, height, title}."""
    from cygnus.apps import cua
    data = cua.call("get_window_state", {"pid": pid, "window_id": int(hwnd),
                                         "include_accessibility_tree": False})
    b64 = data.get("screenshot_png_b64")
    if not b64:
        raise OcrError("the Cua driver returned no picture of that window")
    return {"png": base64.b64decode(b64), "width": data.get("screenshot_width"),
            "height": data.get("screenshot_height"), "title": data.get("window_title") or ""}


def read(pid: int, hwnd: int) -> list[dict]:
    """The window's text lines, read on this PC."""
    return recognize(window_image(pid, hwnd)["png"])


# ----------------------------------------------------------------- finding --

_STOP = {"the", "a", "an", "my", "button", "link", "tab", "menu", "option", "item", "on", "in", "at", "to"}


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (word or "").lower())


def find(lines: list[dict], name: str) -> list[dict]:
    """Where the spoken words appear on screen: [{text, x, y, exact}], x and y
    being the centre of the matched words. The words must be consecutive in
    one line, in order ("save as" is not "as ... save"). A line that is just
    those words is exact; exact hits are returned alone when there are any."""
    want = [w for w in (_norm(t) for t in re.findall(r"\S+", name or "")) if w and w not in _STOP]
    if not want:
        return []
    hits = []
    for line in lines:
        words = [(_norm(w["text"]), w) for w in line["words"]]
        words = [(n, w) for n, w in words if n]
        for start in range(len(words) - len(want) + 1):
            run = words[start:start + len(want)]
            if [n for n, _ in run] != want:
                continue
            boxes = [w for _, w in run]
            left = min(b["x"] for b in boxes)
            top = min(b["y"] for b in boxes)
            right = max(b["x"] + b["w"] for b in boxes)
            bottom = max(b["y"] + b["h"] for b in boxes)
            rest = [n for n, _ in words if n not in _STOP]
            hits.append({"text": " ".join(b["text"] for b in boxes), "x": round((left + right) / 2),
                         "y": round((top + bottom) / 2), "exact": rest == want})
    exact = [h for h in hits if h["exact"]]
    return exact or hits


def changed(before: list[dict], after: list[dict]) -> bool:
    """Whether the window's text changed enough to say something happened:
    a tenth of its lines, or at least two, differ."""
    a = {line["text"] for line in before}
    b = {line["text"] for line in after}
    diff = len(a ^ b)
    return diff >= max(2, len(a | b) // 10)
