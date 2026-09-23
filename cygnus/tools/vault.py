"""cygnus.tools.vault - the Second Brain vault, read only: search (substring and
turbovec semantic), read a note, and project credentials kept in the vault.

Moved out of tools.py (restructure phase 5). Nothing here may write, edit,
delete, move or rename anything under VAULT_ROOT."""

import os
import re
import time
from pathlib import Path

from cygnus.tools import SEARCH_TIME_BUDGET, _redact  # defined before this module is imported


# --- Second Brain vault (READ-ONLY) ------------------------------------------
# READ-ONLY INTEGRATION. Nothing in this module may write, edit, delete, move or
# rename anything under VAULT_ROOT. The vault is the user's personal knowledge
# base; JARVIS may only read from it. Any future write path belongs elsewhere.
# JARVIS_VAULT_ROOT in .env. Most people have no vault, and the tools say
# so plainly rather than pointing into a stranger's Documents.
VAULT_ROOT = Path(os.getenv("JARVIS_VAULT_ROOT",
                            str(Path.home() / "Documents" / "Second Brain")))
# Machine noise and staging areas: they bury real notes in the results.
VAULT_SKIP_DIRS = {".obsidian", ".git", ".trash", "_scratch", "_to_delete"}
# Having a vault is optional; say that rather than only naming a missing folder.
NO_VAULT = (f"[Error] No notes vault at {VAULT_ROOT}, sir. A vault is optional: "
            "set JARVIS_VAULT_ROOT in .env if you keep one.")
VAULT_SKIP_PREFIXES = ("attachments/social-",)


def index_state() -> str:
    """"none", "notes only" or "turbovec - <age>": what vault search can use.

    For the HUD's "Vault index" row, which sat on an em-dash outside demo mode
    because nothing answered it. A stat, never a directory walk.
    """
    try:
        if not VAULT_ROOT.exists():
            return "none"
        index = VAULT_ROOT / "turbovec_index.tv"
        if not index.exists():
            return "notes only"
        days = (time.time() - index.stat().st_mtime) / 86400
        return f"turbovec · {'today' if days < 1 else f'{days:.0f}d old'}"
    except OSError:
        return "unknown"


def _vault_notes():
    """Yield every eligible .md path under the vault. Read-only traversal."""
    for f in VAULT_ROOT.rglob("*.md"):
        rel = f.relative_to(VAULT_ROOT).as_posix()
        if any(part in VAULT_SKIP_DIRS for part in f.relative_to(VAULT_ROOT).parts[:-1]):
            continue
        if any(rel.lower().startswith(p) for p in VAULT_SKIP_PREFIXES):
            continue
        if not f.is_file():
            continue
        yield f


def _vault_text(path: Path) -> str:
    """Read a note, tolerating the odd non-UTF-8 byte. Never writes."""
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def search_vault(query: str, limit: int = 10) -> str:
    """Search the Second Brain vault for text. Case-insensitive, READ-ONLY.

    Scans every .md note under the vault and returns up to `limit` notes, each
    with its path and the first line that matched.
    """
    try:
        if not VAULT_ROOT.exists():
            return NO_VAULT

        needle = query.lower()
        hits = []
        scanned = 0
        deadline = time.monotonic() + SEARCH_TIME_BUDGET
        stopped = None

        for f in _vault_notes():
            if time.monotonic() > deadline:
                stopped = f"stopped after {SEARCH_TIME_BUDGET}s"
                break
            try:
                text = _vault_text(f)
            except Exception:
                continue
            scanned += 1
            rel = f.relative_to(VAULT_ROOT).as_posix()
            matched = needle in rel.lower()
            for n, line in enumerate(text.splitlines(), 1):
                if needle in line.lower():
                    hits.append(f"{rel}:{n}: {_redact(line.strip())[:120]}")
                    matched = False
                    break
            if matched:
                # Filename match with no matching body line still worth reporting.
                hits.append(f"{rel}: (matched the note title)")
            if len(hits) >= limit:
                stopped = f"stopped at the {limit}-note limit"
                break

        note = f" (search {stopped})" if stopped else ""
        if not hits:
            return f"No vault notes matched '{query}' in {scanned} notes searched.{note}"
        return (f"Found '{query}' in {len(hits)} of {scanned} vault notes searched{note}: "
                + " | ".join(hits))
    except Exception as e:
        return f"[Error] {str(e)}"


# --- turbovec semantic search ------------------------------------------------
# The embedding model is loaded ONCE at import time. Loading it per query cost
# 3-8s and was the reason semantic search took ~1:45s on a cache miss.
_DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

try:
    from sentence_transformers import SentenceTransformer
    _EMBEDDING_MODEL = SentenceTransformer(_DEFAULT_EMBEDDING_MODEL)
    _EMBEDDING_DIM = 384
except Exception:
    _EMBEDDING_MODEL = None
    _EMBEDDING_DIM = None

# Any non-default model named in turbovec_meta.json is loaded at most once.
_extra_models = {}


def _normalize_model_name(name: str) -> str:
    """turbovec_meta.json may store the short name ('all-MiniLM-L6-v2')."""
    return name.split("/")[-1] if name else ""


def _get_embedding_model(name: str = None):
    """Return a cached SentenceTransformer. Never loads the same model twice."""
    if _EMBEDDING_MODEL is None:
        return None
    if not name or _normalize_model_name(name) == _normalize_model_name(_DEFAULT_EMBEDDING_MODEL):
        return _EMBEDDING_MODEL
    if name not in _extra_models:
        try:
            _extra_models[name] = SentenceTransformer(name)
        except Exception:
            _extra_models[name] = None
    return _extra_models[name] or _EMBEDDING_MODEL


# Module-level cache for turbovec index + model (avoids reload on every search)
_turbovec_cache = None
_turbovec_cache_vault = None


def _load_turbovec_index(vault_root: Path):
    """Load the persisted turbovec index and embedding model if available.

    Returns (index, model, dim) or None if not available.
    Never mutates the vault — read-only.

    Uses module-level cache so the model + index are only loaded once per
    process. Subsequent calls return the cached tuple instantly.

    NOTE: turbovec 0.8.0's load() has a bug where search() returns empty
    results after loading from disk. We work around this by rebuilding the
    in-memory index from saved embeddings (.npy) — which is just a fast
    prepare()+add() call, no re-embedding needed.
    """
    global _turbovec_cache, _turbovec_cache_vault

    # Return cached result if same vault
    if _turbovec_cache is not None and _turbovec_cache_vault == vault_root:
        return _turbovec_cache

    try:
        import turbovec as tv
        import numpy as np
    except ImportError:
        return None

    if _EMBEDDING_MODEL is None:
        return None

    index_path = vault_root / "turbovec_index.tv"
    if not index_path.exists():
        return None

    # Try the in-memory rebuild approach (works around the load() bug)
    emb_path = vault_root / "turbovec_embeddings.npy"
    notes_path = vault_root / "turbovec_notes.txt"
    # Also check .automation/ subdirectory (where deploy script writes it)
    if not notes_path.exists():
        notes_path = vault_root / ".automation" / "turbovec_notes.txt"
    meta_path = vault_root / "turbovec_meta.json"

    if not emb_path.exists() or not notes_path.exists():
        # Fallback: try the broken load() — may return empty search results
        try:
            import struct
            with open(index_path, "rb") as f:
                header = f.read(16)
                if len(header) >= 8 and header[:4] == b"TVPI":
                    dim = struct.unpack("<H", header[6:8])[0]
                else:
                    dim = 384
        except Exception:
            dim = 384

        model = _get_embedding_model()
        if model is None:
            return None

        try:
            index = tv.TurboQuantIndex(dim)
            try:
                index.prepare()
            except TypeError:
                pass
            index.load(str(index_path))
            _turbovec_cache = (index, model, dim)
            _turbovec_cache_vault = vault_root
            return index, model, dim
        except Exception:
            return None

    # Primary path: rebuild from saved embeddings (works)
    try:
        import struct
        with open(index_path, "rb") as f:
            header = f.read(16)
            if len(header) >= 8 and header[:4] == b"TVPI":
                dim = struct.unpack("<H", header[6:8])[0]
            else:
                dim = 384
    except Exception:
        dim = 384

    # Read metadata for model name
    model_name = _DEFAULT_EMBEDDING_MODEL
    if meta_path.exists():
        import json as _json
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            model_name = meta.get("model", model_name)
            dim = meta.get("dim", dim)
        except Exception:
            pass

    # Embedding model comes from the module-level cache — no per-query load.
    model = _get_embedding_model(model_name)
    if model is None:
        return None

    # Load saved embeddings and rebuild index in-memory
    try:
        embeddings = np.load(str(emb_path))
        if embeddings.shape[1] != dim:
            dim = embeddings.shape[1]
    except Exception:
        return None

    index = tv.TurboQuantIndex(dim)
    try:
        index.prepare()
    except TypeError:
        pass
    index.add(embeddings)

    result = (index, model, dim)
    _turbovec_cache = result
    _turbovec_cache_vault = vault_root
    return result


def search_vault_semantic(query: str, limit: int = 10) -> str:
    """Search the vault semantically using turbovec vector index (READ-ONLY).

    Uses the persisted turboquant index (turbovec_index.tv) if it exists in the
    vault root. Embeds the query with sentence-transformers, then performs a
    SIMD-accelerated nearest-neighbor search — returns results in sub-50ms.

    Falls back to search_vault() if no index is available.
    """
    try:
        if not VAULT_ROOT.exists():
            return NO_VAULT

        result = _load_turbovec_index(VAULT_ROOT)
        if result is None:
            # Fallback to substring search
            return search_vault(query, limit=limit)

        index, model, dim = result

        # Embed the query
        q_vec = model.encode([query], normalize_embeddings=True).astype("float32")

        # Vector search
        scores, indices = index.search(q_vec, k=limit)

        # Build note list from saved paths (must match how index was built)
        notes_path = VAULT_ROOT / "turbovec_notes.txt"
        # Also check .automation/ subdirectory
        if not notes_path.exists():
            notes_path = VAULT_ROOT / ".automation" / "turbovec_notes.txt"
        if notes_path.exists():
            note_rels = notes_path.read_text(encoding="utf-8").strip().split("\n")
            notes = [(rel, VAULT_ROOT / rel.replace("/", "\\")) for rel in note_rels]
        else:
            notes = list(_vault_notes())

        hits = []
        for rank, (score, idx) in enumerate(zip(scores[0], indices[0])):
            if idx >= len(notes):
                continue
            # Handle both tuple (rel, path) and bare Path
            entry = notes[idx]
            if isinstance(entry, tuple):
                rel = entry[0]
                note = entry[1]
            else:
                note = entry
                rel = note.relative_to(VAULT_ROOT).as_posix()

            text = _vault_text(note)
            snippet = ""
            for line in text.splitlines():
                clean = line.strip()
                if len(clean) > 10:
                    snippet = _redact(clean[:120])
                    break
            hits.append(f"{rel} (score={float(score):.4f}): {snippet}")

        if not hits:
            return search_vault(query, limit=limit)

        return (f"Semantic search found {len(hits)} results for '{query}': "
                + " | ".join(hits))

    except Exception as e:
        # Graceful fallback to substring search on any error
        return search_vault(query, limit=limit)


def read_vault_note(name: str) -> str:
    """Read one Second Brain note in full by fuzzy filename match. READ-ONLY.

    Matches any .md note whose path contains `name`, case-insensitive. If more
    than one matches, the candidates are listed instead of guessing.
    """
    try:
        if not VAULT_ROOT.exists():
            return NO_VAULT

        needle = name.lower().removesuffix(".md")
        matches = [f for f in _vault_notes()
                   if needle in f.relative_to(VAULT_ROOT).as_posix().lower()]
        if not matches:
            return f"[Error] No vault note matches '{name}'. Try search_vault first."

        if len(matches) > 1:
            # An exact stem match beats its near-namesakes; only ask when truly ambiguous.
            exact = [f for f in matches if f.stem.lower() == needle]
            if len(exact) == 1:
                matches = exact
            else:
                listed = "; ".join(f.relative_to(VAULT_ROOT).as_posix() for f in matches[:15])
                return (f"{len(matches)} vault notes match '{name}'. "
                        f"Say which one: {listed}")

        note = matches[0]
        text = _vault_text(note)
        rel = note.relative_to(VAULT_ROOT).as_posix()
        if len(text) > 20000:
            text = text[:20000] + "\n... (note truncated)"
        return f"{rel}:\n{text}"
    except Exception as e:
        return f"[Error] {str(e)}"


# --- Credentials stored in the vault (READ-ONLY) ------------------------------
# The credential notes live at wiki/secrets/ inside the vault and are local-only
# (gitignored). Same rule as the rest of the vault integration applies, only more
# so: JARVIS reads these and never writes, edits, moves or renames them.
VAULT_SECRETS_DIR = VAULT_ROOT / "wiki" / "secrets"
# Field names the notes use for the two things callers actually need.
_USERNAME_KEYS = ("username", "user", "login", "account", "email", "user_name")
_PASSWORD_KEYS = ("password", "pass", "pwd", "passphrase")


def _clean_cell(cell: str) -> str:
    """Strip the markdown a table cell is dressed in: bold, code ticks, spaces."""
    return cell.replace("**", "").replace("`", "").strip()


def _parse_credential_table(text: str) -> dict:
    """Pull every 'key | value' row out of a markdown note into a flat dict.

    The notes are hand-written, so nothing here assumes a fixed row order or a
    fixed set of fields - anything beyond username and password comes back as
    extra rather than being dropped.
    """
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c for c in line.split("|")]
        # A well-formed row is '| key | value |', so split gives ['', k, v, ''].
        if len(cells) < 4:
            continue
        key = _clean_cell(cells[1])
        value = _clean_cell(cells[2])
        if not key or not value:
            continue
        if set(key) <= set("-: ") or set(value) <= set("-: "):
            continue  # the ---|--- separator row
        norm = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if norm and norm not in fields:
            fields[norm] = value
    return fields


def get_credentials(project: str) -> dict:
    """Read one credential note from the vault and return its fields. READ-ONLY.

    Returns a dict for programmatic callers - {"ok", "username", "password",
    "extra", "summary"} - where "summary" is safe to speak aloud because the
    password is masked. On failure "ok" is False and "summary" carries the
    error, so a caller can always speak summary without checking anything else.
    """
    try:
        if not VAULT_SECRETS_DIR.exists():
            msg = (f"[Error] No credentials folder in the vault at {VAULT_SECRETS_DIR}. "
                   "The notes live in wiki/secrets.")
            return {"ok": False, "error": msg, "summary": msg}

        needle = project.lower().removesuffix(".md").strip()
        notes = sorted(VAULT_SECRETS_DIR.glob("*.md"))
        exact = [f for f in notes if f.stem.lower() == needle]
        matches = exact or [f for f in notes if needle in f.stem.lower()]
        if not matches:
            names = "; ".join(f.stem for f in notes) or "none"
            msg = (f"[Error] No credential note matches '{project}'. "
                   f"Notes on file: {names}")
            return {"ok": False, "error": msg, "summary": msg}
        if len(matches) > 1:
            names = "; ".join(f.stem for f in matches[:10])
            msg = (f"{len(matches)} credential notes match '{project}'. "
                   f"Say which one: {names}")
            return {"ok": False, "error": msg, "summary": msg}

        note = matches[0]
        fields = _parse_credential_table(_vault_text(note))
        username = next((fields[k] for k in _USERNAME_KEYS if k in fields), None)
        password = next((fields[k] for k in _PASSWORD_KEYS if k in fields), None)
        extra = {k: v for k, v in fields.items()
                 if k not in _USERNAME_KEYS and k not in _PASSWORD_KEYS}

        if username is None and password is None:
            msg = (f"[Error] Found the note {note.stem} but no username or password "
                   "row in it. The table may use different field names.")
            return {"ok": False, "error": msg, "note": note.stem, "extra": extra,
                    "summary": msg}

        # Never put the real password in the spoken summary - it is read aloud.
        parts = [f"username: {username}" if username else "username: not in the note",
                 "password: ***" if password else "password: not in the note"]
        for k in ("url", "server", "environment", "env", "host"):
            if k in extra:
                parts.append(f"{k}: {extra[k]}")
        summary = f"{note.stem} - " + ", ".join(parts)

        return {"ok": True, "note": note.stem, "username": username,
                "password": password, "extra": extra, "summary": summary}
    except Exception as e:
        msg = f"[Error] Could not read credentials for '{project}': {e}"
        return {"ok": False, "error": msg, "summary": msg}


def _get_credentials_tool(**kw) -> str:
    """Tool wrapper: hands back only the redacted summary, never the password."""
    return get_credentials(kw["project"])["summary"]
