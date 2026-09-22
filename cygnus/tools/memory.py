"""cygnus.tools.memory - what Cygnus remembers about the owner: durable facts
in the profile (remember_fact), the semantic fact store, Honcho, and the
memory context handed to the brain and to delegates.

Moved out of tools.py (restructure phase 5)."""

import os
import re
from cygnus import paths
from cygnus.tools.vault import _get_embedding_model


# Phase 18: durable memory writes. "remember X" appends a bullet to
# jarvis-profile.md under ## Remembered — deduped, capped at _REMEMBER_CAP
# lines so the profile injected into JARVIS_SYSTEM can't grow unbounded.
_PROFILE_PATH = str(paths.PROFILE)
_REMEMBER_HEADING = "## Remembered"
_REMEMBER_CAP = 40


def remember_fact(text: str) -> str:
    """Append a durable fact to jarvis-profile.md. Returns a spoken confirmation."""
    fact = re.sub(r"^(please\s+)?(remember( that)?|note that|"
                  r"keep in mind( that)?|don'?t forget( that)?)\s*", "",
                  str(text or "").strip(), flags=re.I).strip().rstrip(".")
    # Phase 23: dedupe — skip if an equivalent line already stored
    try:
        _existing = open(_PROFILE_PATH, encoding="utf-8").read() if os.path.exists(_PROFILE_PATH) else ""
        _norm = " ".join(fact.lower().split())
        if any(_norm == " ".join(l.strip().lstrip("-").strip().lower().split())
               for l in _existing.splitlines() if l.strip().startswith("- ")):
            return f"Noted, sir. I already have that: {fact}."
    except Exception:
        pass
    if not fact:
        return "[Error] Nothing to remember — say 'remember' followed by the fact."
    fact = fact[0].upper() + fact[1:]
    line = f"- {fact}"
    try:
        content = open(_PROFILE_PATH, encoding="utf-8").read() \
                  if os.path.exists(_PROFILE_PATH) else "# JARVIS Profile\n"
    except OSError as e:
        return f"[Error] Could not read profile: {e}"
    if any(l.lower() == line.lower() for l in content.splitlines()):
        return f"Already noted, sir: {fact}."
    if _REMEMBER_HEADING not in content:
        content = content.rstrip("\n") + f"\n\n{_REMEMBER_HEADING}\n{line}\n"
    else:
        lines = content.splitlines()
        idx = lines.index(_REMEMBER_HEADING)
        kept = [l for l in lines[idx + 1:] if l.startswith("- ")]
        kept.append(line)
        kept = kept[-_REMEMBER_CAP:]
        lines = lines[:idx + 1] + kept
        content = "\n".join(lines) + "\n"
    try:
        open(_PROFILE_PATH, "w", encoding="utf-8").write(content)
    except OSError as e:
        return f"[Error] Could not write profile: {e}"
    remember_in_honcho(fact)
    # Semantic fact store: AUDN classification (add/noop/supersede), temporal
    # validity, provenance. Best-effort — the profile bullet above is the
    # durable fallback if the store fails for any reason.
    try:
        from cygnus.memory import semantic as _sm
        _sm.set_embedder(_get_embedding_model_wrapper)
        _sm.set_classifier(_router_audn_classifier)
        res = _sm.add(fact, source="remember")
        if res.get("action") == "supersede":
            return (f"Noted, sir — that updates what I knew. Now: {fact}.")
        if res.get("action") == "noop":
            return f"Already known, sir: {fact}."
    except Exception:
        pass
    return f"Noted, sir. I'll remember: {fact}."


def _get_embedding_model_wrapper(text: str):
    """Bridge so semantic_memory can reuse the cached embedding model."""
    m = _get_embedding_model()
    if m is None:
        return None
    return m.encode([text], normalize_embeddings=True)[0].tolist()


def _router_audn_classifier(new_text: str, old_text: str):
    """LLM arbiter for ambiguous memory classification (the 0.80-0.985 cosine
    band where word-order paraphrases and genuine updates are indistinguishable
    by embedding alone). Returns same|update|different, or None on any failure
    (the caller falls back to the deterministic threshold)."""
    try:
        from openai import OpenAI
        from cygnus.config import ROUTER_BASE_URL, ROUTER_API_KEY, ROUTER_MODEL
        client = OpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY or "dummy",
                        timeout=25, max_retries=0)
        prompt = (
            'Two recorded facts about the same user:\n'
            f'NEW: "{new_text}"\n'
            f'EXISTING: "{old_text}"\n'
            'Is NEW the same fact as EXISTING, an updated version of it that '
            'replaces it, or a different fact? Answer with exactly one word: '
            'same, update, or different.')
        r = client.chat.completions.create(
            model=ROUTER_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3, temperature=0)
        return (r.choices[0].message.content or "").strip().lower()
    except Exception:
        return None


# ── Phase 20b: Honcho memory augmentation (self-hosted, Gemini-only) ───────
_HONCHO_CLIENT = None
_HONCHO_WORKSPACE = "jarvis"
_HONCHO_PEER = "user"

def _get_honcho():
    """Lazy cached Honcho client -> http://localhost:8000. Returns None if unavailable."""
    global _HONCHO_CLIENT
    if _HONCHO_CLIENT is not None:
        return _HONCHO_CLIENT
    try:
        from honcho import Honcho
        _HONCHO_CLIENT = Honcho(workspace_id=_HONCHO_WORKSPACE, base_url="http://localhost:8000")
    except Exception:
        _HONCHO_CLIENT = False  # sentinel: tried, failed
    return _HONCHO_CLIENT if _HONCHO_CLIENT else None

def remember_in_honcho(fact: str) -> None:
    """Best-effort mirror of a remembered fact into Honcho. Never raises."""
    try:
        h = _get_honcho()
        if h is None:
            return
        peer = h.peer(_HONCHO_PEER)
        sess = h.session("profile")
        sess.add_messages([peer.message(fact)])
    except Exception:
        pass  # augmentation only; jarvis-profile.md remains the record

def get_memory_context(tokens: int = 4000) -> str:
    """Budgeted, reasoned memory context from Honcho. Falls back to profile file."""
    try:
        h = _get_honcho()
        if h is None:
            raise RuntimeError("honcho unavailable")
        peer = h.peer(_HONCHO_PEER)
        sess = h.session("profile")
        ctx = sess.context(summary=True, tokens=tokens)
        out = []
        summary = getattr(ctx, "summary", None)
        msgs = getattr(ctx, "messages", []) or []
        # Dedupe messages by normalized content (case/space-insensitive), keep order.
        seen = set(); deduped = []
        for m in msgs:
            c = str(getattr(m, "content", m)).strip()
            key = " ".join(c.lower().split())
            if key and key not in seen:
                seen.add(key); deduped.append(c)
        if summary:
            # Reasoned layer wins; append a few deduped recents for grounding.
            out.append("SUMMARY: " + str(summary))
            if deduped:
                out.append("RECENT:")
                for c in deduped[-5:]:
                    out.append("  - " + c)
        elif deduped:
            # No summary yet (Deriver hasn't concluded) -> deduped recents only.
            out.append("RECENT:")
            for c in deduped[-8:]:
                out.append("  - " + c)
        if out:
            return "\n".join(out)
        raise RuntimeError("empty honcho context")
    except Exception:
        # fallback: raw profile file
        try:
            p = _PROFILE_PATH
            if os.path.exists(p):
                return open(p, encoding="utf-8").read()[-tokens*4:]
        except Exception:
            pass
        return "(no memory context available)"
