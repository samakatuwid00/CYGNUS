"""one_shot.py - a single model call outside a turn.

Memory distilling, the memory arbiter, "describe that window" and the
Apps-panel rules each asked the router's URL directly. With
CYGNUS_PROVIDER=gemini there is no router, so every one of them failed; and
under JARVIS_LOCAL_ONLY they still sent conversation to the cloud every six
hours (2026-09-30). They ask here instead: the router's models, when one is
in use, then Google's, and none of either under local-only.
"""

from __future__ import annotations

# Gemini thinks before it answers, and the thinking counts against
# max_tokens: asked for one word in 3 tokens, gemini-2.5-flash spent them all
# and replied with nothing (2026-09-30). These calls need no reasoning.
_GOOGLE_EXTRA = {"reasoning_effort": "none"}


def candidates(router_models, timeout: float) -> list:
    """(client, model name, is Google) to try in order."""
    from openai import OpenAI

    from cygnus import config
    from cygnus.brain import google_ai
    if config.JARVIS_LOCAL_ONLY:
        return []
    out = []
    if config.JARVIS_USE_9ROUTER:
        router = OpenAI(base_url=config.ROUTER_BASE_URL, api_key=config.ROUTER_API_KEY or "x",
                        timeout=timeout, max_retries=0)
        out += [(router, m, False) for m in dict.fromkeys(m for m in router_models if m)]
    google = google_ai.models()
    if google:
        client = OpenAI(base_url=google_ai.GOOGLE_AI_BASE_URL, api_key=google_ai.GOOGLE_AI_API_KEY,
                        timeout=timeout, max_retries=0)
        out += [(client, google_ai.wire_name(m), True) for m in google]
    return out


def complete(router_models, messages, timeout: float, accept=None, max_models=None, **create) -> tuple:
    """(reply text, model) from the first model whose reply `accept` takes
    (default: any text at all). Raises RuntimeError naming each failure when
    none does."""
    from cygnus.brain import google_ai, router_keeper
    errors, router_down = [], False
    for client, model, is_google in candidates(router_models, timeout)[:max_models]:
        if router_down and not is_google:
            continue
        try:
            reply = client.chat.completions.create(model=model, messages=messages, **create,
                                                   **(_GOOGLE_EXTRA if is_google else {}))
            text = (reply.choices[0].message.content or "").strip()
        except Exception as e:
            errors.append(f"{model}: {type(e).__name__}")
            if is_google:
                google_ai.mark_failed(google_ai.PREFIX + model, str(e))
            elif router_keeper.is_connection_error(e):
                router_down = True          # its other models would fail the same way
            continue
        if text and (accept is None or accept(text)):
            return text, model
        errors.append(f"{model}: {'unusable reply' if text else 'empty reply'}")
    raise RuntimeError("; ".join(errors) or "no model may answer (local-only, or none configured)")
