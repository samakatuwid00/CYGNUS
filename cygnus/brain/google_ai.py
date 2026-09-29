"""google_ai.py - the Gemini API as a model source of the agent loop, next to
9router, with Cygnus's own key (GOOGLE_AI_API_KEY in .env).

On 2026-09-26 every 9router model was down at once (antigravity out of quota
for days, the rest unlicensed, rate-limited or unpaid) and every turn fell to
the local 4B model. Google's OpenAI-compatible endpoint takes the same chat
call with tools, so the loop only needs a second client.

Models carry a "google/" prefix inside Cygnus so they never mix with 9router's
names or its health state. A model that fails rests for a while here, not in
router_health.
"""

from __future__ import annotations

import re
import threading
import time

from cygnus.config import GOOGLE_AI_API_KEY, GOOGLE_AI_BASE_URL, GOOGLE_AI_MODELS

PREFIX = "google/"
REST_S = 60.0             # a model that failed is skipped this long...
QUOTA_REST_S = 600.0      # ...and this long when its quota ran out

_lock = threading.Lock()
_resting: dict[str, float] = {}     # model -> monotonic time it may be tried again
_client = None


def enabled() -> bool:
    return bool(GOOGLE_AI_API_KEY)


def owns(model: str) -> bool:
    return str(model or "").startswith(PREFIX)


def wire_name(model: str) -> str:
    """The name Google knows: 'google/gemini-3.6-flash' -> 'gemini-3.6-flash'."""
    return model[len(PREFIX):] if owns(model) else model


def models(now: float | None = None) -> list[str]:
    """The configured Google models, best first, minus the resting ones."""
    if not enabled():
        return []
    now = time.monotonic() if now is None else now
    with _lock:
        return [PREFIX + m for m in GOOGLE_AI_MODELS if _resting.get(PREFIX + m, 0.0) <= now]


def is_limit(reason) -> bool:
    """A rate or quota limit, as opposed to a broken request."""
    return bool(re.search(r"\b429\b|quota|RESOURCE_EXHAUSTED", str(reason), re.I))


def _until_daily_reset(wall: float | None = None) -> float:
    """Seconds until the free tier's daily quota resets: midnight Pacific."""
    import datetime
    wall = time.time() if wall is None else wall
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        tz = datetime.timezone(datetime.timedelta(hours=-7))
    now = datetime.datetime.fromtimestamp(wall, tz)
    midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(60.0, (midnight - now).total_seconds())


def mark_failed(model: str, reason: str = "", now: float | None = None, wall: float | None = None) -> None:
    """Rest a model after a failed call: a minute, ten when rate-limited, and
    until midnight Pacific when its daily quota is spent. The free tier allows
    20 requests a day per model and a turn makes 2-4 (2026-09-27), so a spent
    model was retried every ten minutes all day."""
    now = time.monotonic() if now is None else now
    reason = str(reason)
    if re.search(r"PerDay", reason):
        rest = _until_daily_reset(wall)
    elif is_limit(reason):
        rest = QUOTA_REST_S
    else:
        rest = REST_S
    with _lock:
        _resting[model] = now + rest


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=GOOGLE_AI_BASE_URL, api_key=GOOGLE_AI_API_KEY, max_retries=0)
    return _client


def _reset_for_tests() -> None:
    global _client
    with _lock:
        _resting.clear()
    _client = None
