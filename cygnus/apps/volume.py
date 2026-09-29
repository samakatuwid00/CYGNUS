"""cygnus.apps.volume - the computer's own (master) volume: read it, set it,
turn it up or down, mute and unmute.

"Turn the volume up to 100%" had no tool at all: the model said to use the
keyboard keys, and the owner's next try was handed to Claude Code as a coding
task (2026-09-24). Volume is a local, instant, safe action like stop_music.

Core Audio's IAudioEndpointVolume is called through ctypes, so this needs no
package (pycaw is optional and not installed on every machine). Where Core
Audio can't be reached it falls back to the volume media keys, which move the
volume 2% a press but can't read it.
"""

import ctypes
import re

KEY_STEP = 2             # percent one volume-key press moves
KEY_PRESSES_FULL = 50    # presses from 100% to 0%
DEFAULT_STEP = 10        # "turn it up"
SMALL_STEP = 6           # "a little louder"
BIG_STEP = 20            # "a lot louder"

_CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
_CLSCTX_ALL = 23
_E_RENDER, _E_CONSOLE = 0, 0
# vtable slots (IUnknown is 0-2)
_ENUM_GET_DEFAULT_ENDPOINT = 4
_DEVICE_ACTIVATE = 3
_VOL_SET_SCALAR, _VOL_GET_SCALAR, _VOL_SET_MUTE, _VOL_GET_MUTE = 7, 9, 14, 15

_VK = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}


# ------------------------------------------------------------ Core Audio --

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(text):
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    return g


def _method(ptr, slot, *argtypes, restype=ctypes.HRESULT):
    """COM method `slot` of the interface at `ptr`. An HRESULT restype raises
    OSError on failure, so every call below is checked."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[slot])
    return lambda *args: fn(ptr, *args)


def _release(ptr):
    if ptr:
        _method(ptr, 2, restype=ctypes.c_ulong)()


class _Endpoint:
    """The default speakers' IAudioEndpointVolume, for one `with` block."""

    def __enter__(self):
        ole32 = ctypes.windll.ole32
        # S_OK or S_FALSE: this thread owns one CoUninitialize. Anything else
        # (already initialised in another mode) still leaves COM usable.
        self._uninit = ole32.CoInitialize(None) in (0, 1)
        self._held = []
        try:
            enum = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_MMDEVICE_ENUMERATOR)), None,
                                        _CLSCTX_ALL, ctypes.byref(_guid(_IID_IMMDEVICE_ENUMERATOR)),
                                        ctypes.byref(enum))
            if hr != 0:
                raise OSError(f"CoCreateInstance failed (0x{hr & 0xFFFFFFFF:08X})")
            self._held.append(enum.value)
            device = ctypes.c_void_p()
            _method(enum.value, _ENUM_GET_DEFAULT_ENDPOINT, ctypes.c_int, ctypes.c_int,
                    ctypes.POINTER(ctypes.c_void_p))(_E_RENDER, _E_CONSOLE, ctypes.byref(device))
            self._held.append(device.value)
            vol = ctypes.c_void_p()
            _method(device.value, _DEVICE_ACTIVATE, ctypes.POINTER(_GUID), ctypes.c_uint32,
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(_guid(_IID_IAUDIO_ENDPOINT_VOLUME)), _CLSCTX_ALL, None, ctypes.byref(vol))
            self._held.append(vol.value)
            self.ptr = vol.value
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        for p in reversed(self._held):
            try:
                _release(p)
            except Exception:
                pass
        self._held = []
        if self._uninit:
            ctypes.windll.ole32.CoUninitialize()
        return False

    def level(self) -> float:
        out = ctypes.c_float()
        _method(self.ptr, _VOL_GET_SCALAR, ctypes.POINTER(ctypes.c_float))(ctypes.byref(out))
        return out.value

    def set_level(self, frac: float) -> None:
        _method(self.ptr, _VOL_SET_SCALAR, ctypes.c_float, ctypes.c_void_p)(
            max(0.0, min(1.0, frac)), None)

    def muted(self) -> bool:
        out = ctypes.c_int()
        _method(self.ptr, _VOL_GET_MUTE, ctypes.POINTER(ctypes.c_int))(ctypes.byref(out))
        return bool(out.value)

    def set_muted(self, on: bool) -> None:
        _method(self.ptr, _VOL_SET_MUTE, ctypes.c_int, ctypes.c_void_p)(1 if on else 0, None)


def _press(key, times=1):
    for _ in range(max(0, int(times))):
        ctypes.windll.user32.keybd_event(_VK[key], 0, 0, 0)
        ctypes.windll.user32.keybd_event(_VK[key], 0, 2, 0)


# --------------------------------------------------------------- actions --

def _pct(frac):
    return int(round(frac * 100))


def read():
    """(percent, muted), or None when Core Audio can't be reached."""
    try:
        with _Endpoint() as ep:
            return _pct(ep.level()), ep.muted()
    except Exception:
        return None


def control(action: str, percent=None) -> str:
    """The tool. action: set (percent = the level), up / down (percent = how
    much, default 10), mute, unmute, get. Returns what happened, for speech."""
    action = (action or "").strip().lower()
    if action not in ("set", "up", "down", "mute", "unmute", "get"):
        return "[Error] Volume action must be set, up, down, mute, unmute or get."
    amount = None
    if percent not in (None, ""):
        digits = re.sub(r"[^\d]", "", str(percent))
        if not digits or int(digits) > 100:
            return "[Error] Say a volume from 0 to 100 percent."
        amount = int(digits)
    if action == "set" and amount is None:
        return "[Error] Say what volume to set, from 0 to 100 percent."
    try:
        with _Endpoint() as ep:
            return _control_endpoint(ep, action, amount)
    except Exception as e:
        print(f"[volume] Core Audio unavailable ({type(e).__name__}: {e}); using the volume keys",
              flush=True)
    try:
        return _control_keys(action, amount)
    except Exception as e:
        return f"[Error] I couldn't change the volume: {e}"


def _control_endpoint(ep, action, amount):
    if action == "get":
        return f"The volume is at {_pct(ep.level())}%{', muted' if ep.muted() else ''}, sir."
    if action in ("mute", "unmute"):
        ep.set_muted(action == "mute")
        return "Muted, sir." if action == "mute" else f"Unmuted, sir. The volume is at {_pct(ep.level())}%."
    if action == "set":
        target = amount
    else:
        step = DEFAULT_STEP if amount is None else amount
        target = _pct(ep.level()) + (step if action == "up" else -step)
    target = max(0, min(100, target))
    ep.set_level(target / 100.0)
    # Asking for sound means wanting to hear it: a louder or set volume on a
    # muted machine would change nothing the owner can hear.
    if action in ("set", "up") and target > 0 and ep.muted():
        ep.set_muted(False)
    now = _pct(ep.level())
    return f"Volume {'set to' if action == 'set' else action + ' to'} {now}%, sir."


def _control_keys(action, amount):
    if action == "get":
        return "[Error] I can't read the volume on this machine, sir."
    if action in ("mute", "unmute"):
        # The mute key toggles and its state can't be read here.
        _press("mute")
        return "Toggled mute, sir."
    if action == "set":
        _press("down", KEY_PRESSES_FULL)
        _press("up", round(amount / KEY_STEP))
        return f"Volume set to {amount}%, sir."
    step = DEFAULT_STEP if amount is None else amount
    _press(action, max(1, round(step / KEY_STEP)))
    return f"Volume {action}, sir."


# ---------------------------------------------------------------- speech --

_LEAD = (r"^(?:(?:please|hey|ok(?:ay)?|now|so|jarvis|cygnus|sir|just|"
         r"(?:can|could|would|will)\s+you)[,!\s]+)*")
_VOLUME_WORD_RE = re.compile(r"\b(volume|sound|audio|speakers?)\b")
_MUSIC_TURN_RE = re.compile(r"\b(turn|lower|raise)\b.{0,20}\bmusic\b|\bmusic\b.{0,20}\b(up|down|louder|quieter|softer)\b")
_UP_RE = re.compile(r"\b(up|increase|raise|higher|louder|boost|crank|max(?:imum)?|full)\b")
_DOWN_RE = re.compile(r"\b(down|decrease|lower|lessen|reduce|quieter|softer|min(?:imum)?)\b")
_BARE_LOUDNESS_RE = re.compile(
    _LEAD + r"(?:(?:make\s+it|a\s+(?:little|bit|tad|lot)|slightly|much|way)\s+)*"
    r"(louder|quieter|softer)(?:\s+(?:please|a\s+(?:little|bit|lot)))*\s*[.!?]*$")
_TURN_IT_RE = re.compile(_LEAD + r"turn\s+it\s+(up|down)(?:\s+(?:a\s+(?:little|bit|lot)|please))*\s*[.!?]*$")
_MUTE_RE = re.compile(
    _LEAD + r"(un)?mute(?:\s+(?:the\s+|my\s+)?(?:volume|sound|audio|computer|pc|laptop|speakers?|system))?"
    r"(?:\s+please)?\s*[.!?]*$")
_GET_RE = re.compile(
    _LEAD + r"(?:what(?:'s|\s+is)\s+(?:the\s+|my\s+)?(?:current\s+)?volume|check\s+(?:the\s+)?volume|"
    r"how\s+loud\s+is\s+(?:it|the\s+volume))\b(?:\s+(?:at|now|set\s+to))*\s*[?.!]*$")
# Not a volume command: asking an agent to build one ("delegate to Claude
# Code that you should have the ability to turn the volume up"), a how-to
# question, the settings screen, or a standing limit ("keep the volume under
# 40%" is a rule for the Rules panel, not a level to set now).
_NOT_A_COMMAND_RE = re.compile(
    r"\b(delegate|claude|codex|gemini|hermes|opencode|code|ability|abilities|feature|implement|"
    r"should|how\s+(?:do|can|to)|why|settings?|panel|mixer|driver|device|output|input|card|"
    r"effects?|file|clip|recorder|speak|talk|voice|remember|rules?|keep|under|below|above|"
    r"never|always|cap|limit|whenever|when|if)\b")
_WORD_NUMBERS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
                 "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
                 "one hundred": 100, "a hundred": 100, "hundred": 100}
_MAX_WORDS = 14


def _digits(t):
    for word, n in _WORD_NUMBERS.items():
        t = re.sub(r"\b" + word + r"\b", str(n), t)
    return t


def _size(t):
    if re.search(r"\b(a\s+(?:little|bit|tad)|slightly)\b", t):
        return SMALL_STEP
    if re.search(r"\b(a\s+lot|much|way)\b", t):
        return BIG_STEP
    return None


def parse(text: str):
    """{"action": ..., "percent": ...} for a spoken volume command, else None.
    "turn the volume up to 100%" -> set 100; "volume down by 20" -> down 20;
    "a bit louder" -> up 6; "mute" -> mute; "what's the volume" -> get."""
    t = " ".join((text or "").lower().replace("’", "'").split()).strip()
    if not t or len(t.split()) > _MAX_WORDS or _NOT_A_COMMAND_RE.search(t):
        return None
    m = _MUTE_RE.match(t)
    if m:
        return {"action": "unmute" if m.group(1) else "mute", "percent": None}
    if _GET_RE.match(t):
        return {"action": "get", "percent": None}
    m = _BARE_LOUDNESS_RE.match(t) or _TURN_IT_RE.match(t)
    if m:
        up = m.group(1) in ("louder", "up")
        return {"action": "up" if up else "down", "percent": _size(t)}
    if not (_VOLUME_WORD_RE.search(t) or _MUSIC_TURN_RE.search(t)):
        return None
    t = _digits(t)
    by = re.search(r"\bby\s+(\d{1,3})\b", t)
    num = re.search(r"\b(\d+)(?!\d)", t)
    up, down = bool(_UP_RE.search(t)), bool(_DOWN_RE.search(t))
    if by and (up or down) and not (up and down):
        return {"action": "up" if up else "down", "percent": int(by.group(1))}
    if num:
        # "turn it up to 100", "volume 40", "set the volume at 30": a level.
        # "volume up 20" with no "to" is a step.
        if (up or down) and not (up and down) and not re.search(r"\b(to|at)\s+\d", t):
            return {"action": "up" if up else "down", "percent": int(num.group(1))}
        return {"action": "set", "percent": int(num.group(1))}
    if re.search(r"\b(max(?:imum)?|full|all\s+the\s+way\s+up)\b", t):
        return {"action": "set", "percent": 100}
    if re.search(r"\bhalf(?:way)?\b", t):
        return {"action": "set", "percent": 50}
    if re.search(r"\b(min(?:imum)?|all\s+the\s+way\s+down)\b", t):
        return {"action": "set", "percent": 0}
    if up != down:
        return {"action": "up" if up else "down", "percent": _size(t)}
    return None
