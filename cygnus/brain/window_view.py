"""cygnus.brain.window_view - Cygnus changing its own window by voice.

The desktop app (desktop/main.js) has three views, the Panel, the Full HUD
and the Mini orb, plus full screen (the full HUD over the taskbar too) and
minimize. The tray and the top bar buttons already switch them; this lets
the owner say it: "go full HUD", "full screen yourself", "mini orb", "back
to the panel", "minimize yourself".

parse() reads the command, answer() is the spoken reply. The server sends
the view to the desktop window after the reply (a "view" message, which
the page hands to window.jarvisDesktop).

Only short commands that name a view or Cygnus itself count: "minimize
Chrome" and "minimize it" are about another window (app_reference), "open
the apps panel" is a HUD panel, and a sentence that only mentions the orb
("CYGNUS should have the capabilities to panel, mini orb, ...") is a task.
"""

import re

# The views desktop/main.js knows (MODES), plus full screen and minimize.
VIEWS = ("sticky", "expanded", "mini", "fullscreen", "minimize")

_LEAD = (r"(?:(?:please|pls|hey|ok(?:ay)?|now|cygnus|jarvis|sir|"
         r"(?:can|could|would|will)\s+you)\s+)*")
_TAIL = r"(?:\s+(?:please|now|sir|cygnus|jarvis|for\s+me))*"
_SELF = r"(?:yourself|your\s+window|you|cygnus|jarvis|the\s+(?:hud|window))"
_VERB = (r"(?:go|switch|change|turn|move|shrink|collapse|expand|open|show(?:\s+me)?|"
         r"bring\s+up|pull\s+up|put|make|get|be|become|back|return|come\s+back|"
         r"use|give\s+me|display)")
_NOUN = {
    "expanded": r"full[\s-]*(?:hud|hd|view|size)|(?:the\s+)?hud|big\s+(?:view|mode|screen)",
    "mini": r"mini(?:\s+(?:orb|panel|mode|view|size))?|(?:the\s+|an?\s+)?orb|black\s+hole|small\s+orb",
    "sticky": r"(?:the\s+|a\s+|your\s+)?(?:sticky\s+|small\s+|normal\s+)?panel|normal\s+(?:size|view|mode)",
}

_MINIMIZE_RE = re.compile(
    _LEAD + r"(?:(?:minimi[sz]e|hide)(?:\s+" + _SELF + r")?(?:\s+(?:to|in|into)\s+the\s+(?:tray|taskbar))?"
    r"|go\s+(?:to|into)\s+the\s+(?:tray|taskbar)|(?:go\s+)?hide)" + _TAIL)
# Full screen is the full HUD over the whole display, taskbar too. Bare "full
# screen" may be a video's, so it needs "go" or Cygnus named: "go full screen",
# "full screen yourself", "make yourself full screen".
_FS = r"full[\s-]*screen(?:\s+(?:mode|view))?"
_OWN = r"(?:yourself|your\s+window|the\s+hud)"
_FULLSCREEN_RE = re.compile(
    _LEAD + r"(?:(?:(?:go|switch|turn|put|make|get|expand)\s+)?(?:(?:to|into|in)\s+)?" + _FS + r"\s+" + _OWN
    + r"|(?:go|switch\s+to|turn)\s+" + _FS
    + r"|(?:make|put|turn|expand|switch|set|go)\s+" + _OWN + r"\s+(?:(?:to|into|in)\s+)?" + _FS
    + r")" + _TAIL)
# Leaving it, back to the full HUD. Only with Cygnus named: "exit full
# screen" alone is as likely a video's. Escape leaves it too (main.js).
_UNFULLSCREEN_RE = re.compile(
    _LEAD + r"(?:exit|leave|quit|get\s+out\s+of|come\s+out\s+of)\s+(?:your\s+" + _FS
    + r"|" + _FS + r"\s+" + _OWN + r")" + _TAIL)
_EXPAND_RE = re.compile(_LEAD + r"(?:expand|maximi[sz]e|enlarge)\s+" + _SELF + _TAIL)
_SHRINK_RE = re.compile(_LEAD + r"(?:shrink|collapse)\s+" + _SELF + _TAIL)
_RESTORE_RE = re.compile(_LEAD + r"(?:restore|show)\s+" + _SELF + _TAIL)
_NOUN_RE = {
    view: re.compile(
        _LEAD + r"(?:" + _VERB + r"\s+)?(?:" + _SELF + r"\s+)?(?:back\s+)?"
        r"(?:(?:to|into|in|as|on)\s+)?(?:the\s+|a\s+|your\s+)?"
        r"(?:" + noun + r")(?:\s+(?:mode|view))?(?:\s+(?:yourself|your\s+window))?" + _TAIL)
    for view, noun in _NOUN.items()
}


def _norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"[^\w\s'-]", " ", t)
    return " ".join(t.split())


def parse(text: str):
    """The view a short self-command asks for ('sticky', 'expanded', 'mini',
    'fullscreen' or 'minimize'), or None when the words are not one."""
    t = _norm(text)
    if not t or len(t.split()) > 9:
        return None
    if _MINIMIZE_RE.fullmatch(t):
        return "minimize"
    if _FULLSCREEN_RE.fullmatch(t):
        return "fullscreen"
    if _UNFULLSCREEN_RE.fullmatch(t) or _EXPAND_RE.fullmatch(t):
        return "expanded"
    if _SHRINK_RE.fullmatch(t):
        return "mini"
    if _RESTORE_RE.fullmatch(t):
        return "sticky"
    for view in ("expanded", "mini", "sticky"):
        if _NOUN_RE[view].fullmatch(t):
            return view
    return None


# Set by the server: True while a desktop app window is connected. None
# (tests, the CLI) is taken as "there is one".
desktop_open = None


def answer(view: str) -> str:
    """What Cygnus says as it switches."""
    try:
        present = True if desktop_open is None else bool(desktop_open())
    except Exception:
        present = True
    if not present:
        return ("The panel, the full HUD and the orb are views of the desktop app, sir, "
                "and it isn't open. Start Cygnus from the Start menu or press Control Alt J.")
    return {
        "sticky": "Back to the panel, sir.",
        "expanded": "Going full HUD, sir.",
        "fullscreen": "Going full screen, sir. Press Escape to leave it.",
        "mini": "Shrinking to the orb, sir. Click it or say Cygnus to bring me back.",
        "minimize": "Minimizing, sir. Say Cygnus or press Control Alt J to bring me back.",
    }[view]
