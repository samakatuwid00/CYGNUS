"""context_assembler.py — Phase 4: Context-assembly layer.

Before JARVIS spawns any real agent (hermes, opencode, cli), it pulls only
relevant facts from its own memory, packages them as a self-contained brief
formatted for that delegate's interface, and logs the brief.

Design:
  - pull minimal relevant facts: profile (tier2) + recent window (tier3) + vault semantic (tier1) + app context
  - format per delegate: hermes gets full grounding + cite rule; opencode gets repo-focused; gemini/chatgpt get ideation-focused
  - log brief to audit.jsonl (truncated) and return full brief string
  - delegates can request missing piece via 'clarify' skill (future); for now they say "missing X"

Stdlib only; fails open (returns empty brief on error).
"""

import os
import re
import json
from pathlib import Path
from cygnus import paths

_PROFILE_PATH = paths.PROFILE
def _vault_hint() -> str:
    """The vault, named only when the owner actually has one."""
    from cygnus.tools import vault
    if not vault.VAULT_ROOT.is_dir():
        return ""
    return (f"Second Brain vault at {vault.VAULT_ROOT} (semantic search via turbovec; "
            "read/write wiki/evergreen).")

def _load_profile() -> str:
    try:
        return _PROFILE_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return "(profile not found)"

def _recent_summary(n=6) -> str:
    try:
        from cygnus.memory.session_store import recent_summary as rs
        return rs(n)
    except Exception:
        return ""

def _app_context(task: str) -> str:
    try:
        from cygnus.tools import _app_context as ac
        return ac(task)
    except Exception:
        return ""

# A semantic hit below this cosine score is noise: the search always returns
# its top k, so a note about the router went to Claude Code as "context" for
# a chat-box fix, and Hermes was told to cite it (2026-09-24).
VAULT_MIN_SCORE = 0.30
_HIT_SCORE = re.compile(r"\(score=([\d.]+)\)")


def _keep_scored_hits(result: str) -> str:
    """The semantic hits at or above VAULT_MIN_SCORE, one per line. A result
    with no scores (the substring fallback) is returned as it came; one whose
    hits all score low becomes empty, so no [VAULT CONTEXT] block is written."""
    if "(score=" not in result:
        return result
    _head, _, body = result.partition(": ")
    kept = []
    for hit in body.split(" | "):
        hit = hit.strip()
        if not hit:
            continue
        m = _HIT_SCORE.search(hit)
        if m is None or float(m.group(1)) >= VAULT_MIN_SCORE:
            kept.append(hit)
    return "\n".join(kept)


def _vault_context(task: str, max_hits=3, delegate: str = "hermes") -> str:
    """Pull vault facts relevant to task. Delegate-specific truncation."""
    # For file/opencode delegates, prioritize file-related notes; for ideation, broader.
    try:
        # Use semantic search if available, else keyword
        result = ""
        try:
            from cygnus.tools import execute_tool as _et
            # need to avoid recursion via delegate loop; call search directly
            from cygnus import tools as t
            # Try semantic first
            result = t.search_vault_semantic(task, limit=max_hits) if hasattr(t, "search_vault_semantic") else t.search_vault(task, limit=max_hits)
        except Exception:
            pass
        if result and "[Error" not in result[:20] and "no results" not in result.lower() and "No vault" not in result:
            txt = _keep_scored_hits(result.strip())
            if not txt:
                return ""
            # per-delegate budget: hermes gets 2400, opencode gets 1800, cli gets 1200
            budget = {"hermes": 2400, "opencode": 1800, "gemini-cli": 1200, "claude-cli": 1200, "codex-cli": 1200}.get(delegate, 1800)
            if len(txt) > budget:
                txt = txt[:budget] + "\n...(truncated)"
            return f"[VAULT CONTEXT]\n{txt}\n[END VAULT]\n"
    except Exception:
        pass
    return ""

# CLI agents are named by their cli_agents.json key; the brief styles below
# use the -cli names, so Claude Code was getting Hermes's brief.
_CLI_BRIEF_NAMES = {"claude": "claude-cli", "gemini": "gemini-cli", "codex": "codex-cli"}


def _brief_parts(task: str, delegate: str = "hermes") -> dict:
    """The pieces of a delegate's brief: `title`, `instructions` (one line for
    the delegate's kind) and `background` (profile, session, facts, vault, app
    context). assemble_brief and compose_prompt lay them out differently."""
    delegate = _CLI_BRIEF_NAMES.get(delegate, delegate)
    profile = _load_profile()
    recent = _recent_summary(6)
    appctx = _app_context(task)
    vault = _vault_context(task, max_hits=3, delegate=delegate)
    # recall-shaped queries also pull past session matches
    facts = ""
    try:
        from cygnus.memory import semantic as smod
        block = smod.context_block(10)
        if block:
            facts = block + "\n"
    except Exception:
        pass
    recall = ""
    if re.search(r"\b(remember|last time|did i|what did i|previously|before|history)\b", task, re.I):
        try:
            from cygnus.memory import session_index as si
            si.rebuild()
            hits = si.search_sessions(task, 3)
            if hits:
                recall = "[PAST SESSION MATCHES]\n" + si.format_hits(hits) + "\n\n"
        except Exception:
            pass

    # Per-delegate formatting
    if delegate in ("opencode", "claude-cli", "codex-cli"):
        title = f"CONTEXT for CODE delegate ({delegate}):"
        instructions = ("Instructions: Focus on codebase; read repo files before editing; "
                        "cite files touched; verify build.")
        background = (f"[PROFILE]\n{profile}\n\n[RECENT]\n{recent}\n\n{facts}{recall}{vault}"
                      f"[APP CONTEXT]\n{appctx}\n")
        tail = "\n\n"
    elif delegate in ("gemini-cli", "chatgpt"):
        title = f"CONTEXT for IDEATION delegate ({delegate}):"
        instructions = "Instructions: Provide ideas, alternatives, citations; do not mutate files unless asked."
        background = f"[PROFILE]\n{profile}\n\n[RECENT]\n{recent}\n\n{facts}{recall}{vault}"
        tail = "\n\n"
    else:  # hermes floor
        title = "CONTEXT (JARVIS persistent memory):"
        instructions = ("Ground factual answers in Second Brain vault and cite source note. "
                        "If vault has nothing, say so. Respect [PROFILE] preferences.")
        background = (f"[PROFILE]\n{profile}\n\n[RECENT SESSION]\n{recent}\n\n{facts}{recall}{vault}"
                      f"[KNOWLEDGE BASE] {_vault_hint()}\n")
        tail = "\n"
        if appctx:
            background += appctx + "\n"
    return {"delegate": delegate, "title": title, "instructions": instructions,
            "background": background, "tail": tail,
            "vault_len": len(vault), "appctx_len": len(appctx)}


def _log_brief(parts: dict, task: str, shape: str, text: str) -> None:
    """The brief (truncated) on the audit trail; never raises."""
    try:
        from cygnus.tools import _audit_log
        _audit_log(f"context:{parts['delegate']}", task[:200], shape, result=text[:1000],
                   extra={"delegate": parts["delegate"], "vault_len": parts["vault_len"],
                          "appctx_len": parts["appctx_len"]})
    except Exception:
        pass


def assemble_brief(task: str, delegate: str = "hermes") -> str:
    """Build per-delegate brief and log it. Returns brief prefix to prepend to task.

    The older layout, context first and the task last. The specialist paths
    (OpenCode, Pi) still use it; the CLI agents and Hermes get compose_prompt.
    """
    parts = _brief_parts(task, delegate)
    header = f"{parts['title']}\n{parts['background']}{parts['instructions']}{parts['tail']}TASK:\n"
    _log_brief(parts, task, "brief_assembled", header)
    return header


def compose_prompt(task: str, delegate: str = "hermes", raw_task: str | None = None) -> str:
    """The whole prompt a delegate runs: the task restated as a brief (goal,
    scope, done-when, constraints; task_brief.restate), the owner's own words
    under it, and the profile/session/vault background last, marked as
    possibly irrelevant. Before 2026-09-24 the task came after 2-4 KB of
    context, and the agent got the spoken words unrestated."""
    from cygnus.brain import task_brief
    raw = (raw_task or task or "").strip()
    stated = task_brief.restate(task, raw)
    parts = _brief_parts(task, delegate)
    text = task_brief.compose(stated, raw, f"{parts['title']}\n{parts['instructions']}",
                              parts["background"])
    _log_brief(parts, raw, "prompt_composed", text)
    return text

def delegate_needs_clarification(reply: str) -> bool:
    """True if delegate output indicates missing context and wants clarification."""
    if not reply:
        return False
    t = reply.lower()
    return any(k in t for k in ("missing context", "need more info", "clarify", "which file", "which project", "not enough information"))
