"""voice_ducking.py — duck music while Cygnus speaks and while it listens
(Windows Core Audio, through pycaw).

Ducks per-process audio sessions (Spotify) to a low volume for the duration of
Cygnus's speech and restores the original levels afterwards. Browsers are NOT
ducked for speech: the HUD in a browser tab plays Cygnus's voice through the
browser, so a browser-wide duck would silence Cygnus itself.

While the HUD records a command, browsers are ducked too (duck(browsers=True)):
nothing of Cygnus's plays then, and music from Brave or Chrome was the loudest
thing in the clip. On the owner's recorded commands, music 5 dB under the
voice made 64% of the words wrong and 75% of the app names (2026-09-26).
The caller restores them as soon as the clip is in, before any reply plays.

Failsafe: duck() arms a max-duration timer; if the matching end never arrives
(client crash, lost message), volume is restored automatically.

Needs pycaw (optional). Without it every call does nothing.
"""
from __future__ import annotations

import threading
import time

# Processes whose audio gets ducked when Cygnus speaks.
_DUCK_PROCESSES = ("spotify.exe",)
# ...and, while it listens, these too.
_BROWSER_PROCESSES = ("brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe")
DUCK_VOLUME = 0.15          # fraction of full volume while ducked
FAILSAFE_S = 180.0          # absolute max duck duration

_lock = threading.Lock()
_ducked = False             # True between duck() and unduck()
_originals: dict = {}       # (process name, pid) -> (volume control, original level)
_failsafe_at = 0.0


def _process_name(session) -> str:
    """pycaw's AudioSession.Process is a psutil.Process, or None for the
    system sounds session."""
    proc = getattr(session, "Process", None)
    return (proc.name() if proc else "").lower()


def _iter_duck_sessions(names=_DUCK_PROCESSES):
    """Yield ((process name, pid), session) for sessions of the named processes."""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return
    for s in sessions:
        try:
            name = _process_name(s)
        except Exception:
            continue
        if name in names:
            yield (name, getattr(s, "ProcessId", id(s))), s


def duck(browsers: bool = False) -> bool:
    """Duck music processes (and browsers, when listening). Returns True if
    anything is ducked. Sessions already ducked are left alone, so a second
    call only adds what the first did not cover."""
    global _ducked, _failsafe_at
    names = _DUCK_PROCESSES + (_BROWSER_PROCESSES if browsers else ())
    with _lock:
        for key, s in _iter_duck_sessions(names):
            if key in _originals:
                continue
            try:
                vol = s.SimpleAudioVolume
                _originals[key] = (vol, vol.GetMasterVolume())
                vol.SetMasterVolume(DUCK_VOLUME, None)
            except Exception:
                continue
        if not _originals:
            return False
        _ducked = True
        _failsafe_at = time.monotonic() + FAILSAFE_S
        return True


def unduck() -> None:
    """Restore original volumes (no-op if not currently ducked).

    Restores through the sessions live now, matched by process: Chrome
    replaces its audio session, the handle saved at duck() then points at the
    old one, the restore did nothing and Chrome stayed at 15% (2026-09-26).
    The saved handle is only the fallback."""
    global _ducked
    with _lock:
        if not _ducked:
            return
        names = _DUCK_PROCESSES + _BROWSER_PROCESSES
        live = {}
        for key, s in _iter_duck_sessions(names):
            live.setdefault(key, []).append(s)
        for key, (vol, level) in list(_originals.items()):
            done = False
            for s in live.get(key, []):
                try:
                    s.SimpleAudioVolume.SetMasterVolume(level, None)
                    done = True
                except Exception:
                    continue
            if not done:
                try:
                    vol.SetMasterVolume(level, None)
                except Exception:
                    pass
        _originals.clear()
        _ducked = False


def check_failsafe() -> None:
    """Undo ducking if the failsafe window expired (call periodically)."""
    with _lock:
        expired = _ducked and time.monotonic() > _failsafe_at
    if expired:
        unduck()


def _reset_for_tests() -> None:
    global _ducked, _failsafe_at
    with _lock:
        _originals.clear()
        _ducked = False
        _failsafe_at = 0.0
