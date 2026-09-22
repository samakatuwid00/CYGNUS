"""cygnus.server.apps_routes - the Apps panel's endpoints (/apps, /apps/*,
/apps.html, /shadow/*, /prefs): installed apps and sites, their rules and
abilities, scans, registration and hiding.

On an APIRouter that server/app.py includes where these routes used to be
defined, so the route order is unchanged. Moved out of server/app.py
(restructure phase 5)."""

import asyncio
import json
import os
import threading

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from cygnus import paths
from cygnus import tools
from cygnus.server.app import _no_cache  # defined before this module is imported

router = APIRouter()


DELEGATE_REGISTRY_PATH = str(paths.DELEGATE_REGISTRY)


def _website_items():
    """Registered sites from web_registry.json as /apps items.

    Returns (items, hidden_count). Sites carry kind="website" and a `url`, so
    the panel can badge them and offer "Open Site" instead of a binary launch.
    """
    global _WEBSITE_KEYS
    items, hidden = [], 0
    try:
        from cygnus.apps import web_registry as wr
        sites = (wr.load_registry() or {}).get("sites", {})
    except Exception:
        return [], 0
    for key, site in (sites or {}).items():
        if not isinstance(site, dict):
            continue
        if site.get("hidden"):
            hidden += 1
            continue
        items.append({
            "name": site.get("name") or key,
            "key": key,
            "bin": "",
            "url": site.get("url", ""),
            "host": site.get("host", ""),
            "visits": site.get("visits", 0),
            "category": "website",
            "kind": "website",
            "broken": not bool(site.get("url")),
            "enabled": site.get("enabled", True),
            "adapter": "browser",
            "registered": site.get("registered", True),
            "hidden": False,
            "rule_drafts": site.get("rule_drafts") or [],
            "compiled_rules": site.get("compiled_rules") or [],
        })
    _WEBSITE_KEYS = frozenset(i["key"] for i in items)
    return items, hidden


def _capability_items():
    """Delegates from delegate_registry.json as /apps items (kind="capability").

    These are not launchable — they are what JARVIS can hand a task to — so the
    panel renders them read-only.
    """
    try:
        with open(DELEGATE_REGISTRY_PATH, "r", encoding="utf-8") as f:
            delegates = (json.load(f) or {}).get("delegates", [])
    except Exception:
        return []
    items = []
    for d in delegates or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        did = d["id"]
        items.append({
            "name": did,
            # Namespaced so a delegate id can never collide with an app key.
            "key": "capability:%s" % did,
            "id": did,
            "bin": "",
            "category": "capability",
            "kind": "capability",
            "handles": d.get("handles") or [],
            "description": d.get("description", ""),
            "priority": d.get("priority", 0),
            "tools": d.get("tools") or [],
            "broken": False,
            "enabled": d.get("enabled", True),
            "adapter": d.get("kind", "local"),
            "registered": True,
            "hidden": False,
            "rule_drafts": [],
            "compiled_rules": [],
        })
    items.sort(key=lambda i: (-i["priority"], i["name"]))
    return items


@router.get("/apps")
async def get_apps():
    """The app registry for the HUD's Apps modal.

    Returns two views of the same scan:
    - `curated`: registered + not hidden — the short list the panel opens on.
    - `all`: every detected app that is not hidden — the raw scan, so the user
      can find something the curation missed (and hide the bloatware).
    `apps` stays as an alias of `curated` for older callers.

    Both views also carry the registered websites (kind="website") and the
    delegate capabilities (kind="capability"), so the panel is one surface for
    everything JARVIS can act on.
    """
    import os as _os
    from cygnus.apps.capabilities import load_registry
    # Phase 8: opt-in flags (registered/enabled) come from curate.py
    try:
        from cygnus.apps.curate import is_registered, is_hidden
    except Exception:
        is_registered = lambda k: False
        is_hidden = lambda k: False
    websites, site_hidden = _website_items()
    capabilities = _capability_items()
    extras = websites + capabilities
    reg = load_registry()
    if not reg:
        # A fresh machine: no apps yet. The panel offers Add app / Full scan.
        return JSONResponse({"apps": list(extras), "curated": list(extras),
                             "all": list(extras), "count": len(extras),
                             "curated_count": len(extras),
                             "all_count": len(extras),
                             "hidden_count": site_hidden,
                             "website_count": len(websites),
                             "capability_count": len(capabilities),
                             "websites": websites,
                             "capabilities": capabilities})
    curated = []
    all_apps = []
    hidden_count = 0
    for key, entry in reg.get("apps", {}).items():
        if not isinstance(entry, dict):
            continue
        if is_hidden(key):
            hidden_count += 1
            continue
        binp = entry.get("bin") or ""
        registered = is_registered(key)
        item = {
            "name": entry.get("name", key),
            "key": key,
            "bin": binp,
            "category": entry.get("category", "other"),
            "kind": entry.get("kind", "gui"),
            "broken": bool(binp) and not _os.path.exists(binp),
            "enabled": entry.get("enabled", True),
            "adapter": entry.get("kind"),
            "registered": registered,
            "hidden": False,
            "rule_drafts": entry.get("rule_drafts") or [],
            "compiled_rules": entry.get("compiled_rules") or [],
            "abilities": entry.get("abilities") or [],
            "app_kind": entry.get("app_kind"),
            "deep_scan": entry.get("deep_scan"),
        }
        all_apps.append(item)
        if registered:
            curated.append(item)
    sort_key = lambda a: (a["category"], a["name"])
    curated.sort(key=sort_key)
    all_apps.sort(key=sort_key)
    # Websites and capabilities are curated by construction: they appear in
    # both views, after the installed apps.
    curated += extras
    all_apps += extras
    hidden_count += site_hidden
    return JSONResponse({
        "count": len(curated),
        "curated_count": len(curated),
        "all_count": len(all_apps),
        "hidden_count": hidden_count,
        "website_count": len(websites),
        "capability_count": len(capabilities),
        "generated": reg.get("generated"),
        "curated": curated,
        "all": all_apps,
        "websites": websites,
        "capabilities": capabilities,
        "apps": curated,
    })


@router.post("/apps/open")
async def post_apps_open(message: Request):
    """Open an app by registry key from the HUD modal. Same chain as voice."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    loop = asyncio.get_running_loop()
    # Websites carry kind="website" + url; open them via open_site, not open_application
    # (which expects a binary and would fail on an empty bin with WinError 2).
    if key in _WEBSITE_KEYS:
        result = await loop.run_in_executor(None, lambda: tools.open_site(key))
    else:
        result = await loop.run_in_executor(
            None, lambda: tools.execute_tool("open_application", {"app": key}))
    ok = not result.startswith("[Error]")
    return JSONResponse({"ok": ok, "result": result})


@router.post("/apps/toggle")
async def post_apps_toggle(message: Request):
    """Enable or disable a registered app in the registry."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    enable = bool(body.get("enabled", True))
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    from cygnus.apps.capabilities import REGISTRY_PATH
    reg_path = REGISTRY_PATH
    if not os.path.exists(reg_path):
        return JSONResponse({"error": "no registry"}, status_code=400)
    with open(reg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    apps = data.setdefault("apps", {})
    if key not in apps:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    apps[key]["enabled"] = enable
    tmp = reg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, reg_path)
    return JSONResponse({"ok": True, "key": key, "enabled": enable})


@router.post("/apps/rules")
async def post_apps_rules(message: Request):
    """Store rule drafts (and optional compiled rules) for a registered app."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    drafts = body.get("rule_drafts") or []
    # Key presence, not truthiness: an explicit `compiled_rules: []` commits an
    # empty set (wipes the rules); omitting it only stores drafts.
    compiled = body.get("compiled_rules")
    from cygnus.apps.capabilities import REGISTRY_PATH
    reg_path = REGISTRY_PATH
    if not os.path.exists(reg_path):
        return JSONResponse({"error": "no registry"}, status_code=400)
    with open(reg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    apps = data.setdefault("apps", {})
    if key not in apps:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    if isinstance(compiled, list):
        from cygnus.rules import compiler as rules_compiler
        rules_compiler.commit_rules(key, compiled, reg_path, accept=True)
    else:
        apps[key]["rule_drafts"] = drafts
        tmp = reg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, reg_path)
    return JSONResponse({"ok": True, "key": key})


@router.post("/apps/rules/clear")
async def post_apps_rules_clear(message: Request):
    """Remove one committed rule (`rule_id`) or, when it is absent, every rule.

    Body: {"key": "spotify", "rule_id": "keep_it_quiet"}  — rule_id optional.
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    rule_id = (body.get("rule_id") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    if not _known_app(key):
        return JSONResponse({"error": "unknown app"}, status_code=400)
    from cygnus.apps.capabilities import REGISTRY_PATH
    from cygnus.rules import compiler as rules_compiler
    try:
        removed, remaining = rules_compiler.remove_rule(key, rule_id, REGISTRY_PATH)
    except KeyError:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    if rule_id and not removed:
        return JSONResponse({"error": "unknown rule_id", "key": key, "rule_id": rule_id},
                            status_code=404)
    return JSONResponse({"ok": True, "key": key, "removed": removed, "remaining": remaining})


@router.post("/apps/register")
async def post_apps_register(message: Request):
    """Phase 8: opt an app in (or out) of the curated registry."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    registered = bool(body.get("registered", True))
    enabled = bool(body.get("enabled", True))
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    from cygnus.apps import curate
    ok = curate.mark_registered(key, registered=registered, enabled=enabled)
    if not ok:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    return JSONResponse({"ok": True, "key": key,
                         "registered": registered, "enabled": enabled})


@router.post("/apps/compile")
async def post_apps_compile(message: Request):
    """Compile with JARVIS: turn the drafts into a proposed rule set with AI.

    Each draft line may hold a command and what it should do ('search movies
    in brave -> search hollymoviehd.cc'). rules_ai reads the intent (router,
    then local Ollama); lines no model can read keep the keyword parser. A
    line that already has a structured rule is kept, and an older plain rule
    is recompiled with its saved action so the site it named is not lost.
    Returned `notes` say what JARVIS guessed. User owns the commit
    (POST /apps/rules).
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    drafts = body.get("rule_drafts") or []
    if not key or not drafts:
        return JSONResponse({"error": "missing key or drafts"}, status_code=400)

    from cygnus.rules import ai as rules_ai
    from cygnus.apps.capabilities import REGISTRY_PATH
    entry = {}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            entry = (json.load(f).get("apps") or {}).get(key) or {}
    except Exception:
        pass
    from cygnus.rules import sim as rules_sim

    def _compile():
        say = rules_sim.reporter(key)
        try:
            say("Understanding your rules...", 10)
            rules, notes = rules_ai.compile_drafts(
                key, drafts, entry.get("compiled_rules") or [], entry)
            rules = rules_sim.simulate_all(key, rules, entry, progress=say)
            searches = [r for r in rules if (r.get("action") or {}).get("type") == "search_site"]
            if searches and all((r.get("verification") or {}).get("status") == "verified"
                                for r in searches):
                notes = [n for n in notes if "guessed" not in n]
            return rules, notes
        finally:
            rules_sim.finish_progress(key)

    # Model calls and real page checks take seconds: keep them off the event loop.
    proposed, notes = await asyncio.to_thread(_compile)
    return JSONResponse({
        "ok": True,
        "key": key,
        "proposed": proposed,
        "questions": notes,
        "notes": notes,
        "proposal_markdown": rules_ai.propose_markdown(key, proposed, notes),
    })


def _known_app(key):
    """True when `key` names an app in the on-disk registry."""
    from cygnus.apps.capabilities import REGISTRY_PATH
    if not os.path.exists(REGISTRY_PATH):
        return False
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return key in (data.get("apps") or {})


@router.post("/apps/rules/begin")
async def post_apps_rules_begin(message: Request):
    """Phase 4: start voice rule authoring for an app.

    Body: {"app_key": "spotify", "phrase": "no explicit stuff and keep it quiet"}
    Returns either a clarification question or a proposal awaiting confirmation.
    Nothing is written here — the user owns the commit (/apps/rules/clarify).
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("app_key") or body.get("key") or "").strip()
    phrase = (body.get("phrase") or "").strip()
    if not key:
        return JSONResponse({"error": "missing app_key"}, status_code=400)
    if not phrase:
        return JSONResponse({"error": "missing phrase"}, status_code=400)
    if not _known_app(key):
        return JSONResponse({"error": "unknown app"}, status_code=400)

    from cygnus.rules import voice as rules_voice
    step = await asyncio.to_thread(rules_voice.begin_rule_setup, key, phrase)
    if step.get("status") == "error":
        return JSONResponse(step, status_code=400)
    return JSONResponse(dict(step, ok=True))


@router.post("/apps/rules/clarify")
async def post_apps_rules_clarify(message: Request):
    """Phase 4: answer one clarification question, or confirm the proposal.

    Body: {"app_key": "spotify", "rule_id": "keep_it_quiet", "answer": "under 40%"}
    An empty `rule_id` means "accept the proposal as-is". Once nothing is open
    the finalized rule set is committed to the registry.
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("app_key") or body.get("key") or "").strip()
    rule_id = (body.get("rule_id") or "").strip()
    answer = (body.get("answer") or "").strip()
    if not key:
        return JSONResponse({"error": "missing app_key"}, status_code=400)
    if not answer:
        return JSONResponse({"error": "missing answer"}, status_code=400)

    from cygnus.rules import voice as rules_voice
    step = await asyncio.to_thread(rules_voice.handle_clarification, key, rule_id, answer)
    if step.get("status") == "error":
        return JSONResponse(step, status_code=400)
    return JSONResponse(dict(step, ok=True))


@router.post("/apps/rules/test")
async def post_apps_rules_test(message: Request):
    """Run a command rule once, for real, before or after saving it.

    Body: {"app_key": "brave", "sample": "Dune", "rule_id": ""} — an empty
    rule_id means the proposal in progress. Opens the browser exactly like
    the spoken command would, so the user sees where the rule lands.
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("app_key") or body.get("key") or "").strip()
    rid = (body.get("rule_id") or "").strip()
    sample = (body.get("sample") or "").strip()
    if not key:
        return JSONResponse({"error": "missing app_key"}, status_code=400)
    from cygnus.rules import engine as rules_engine
    from cygnus.rules import voice as rules_voice
    rule = None
    pend = rules_voice.pending_setup(key)
    if not rid and pend:
        rule = pend.get("proposed_rule")
    if rule is None and rid:
        rule = next((r for r in rules_voice.list_rules(key).get("rules") or []
                     if r.get("rule_id") == rid), None)
    if not rule:
        return JSONResponse({"error": "no rule to test"}, status_code=404)
    action = rules_engine.action_of(rule, key, rules_voice._app_entry(key))
    if not action:
        return JSONResponse({"error": "this rule doesn't open anything to test"},
                            status_code=400)
    if action.get("type") in ("search_site", "steps") and not sample:
        from cygnus.rules import sim as rules_sim
        sample = rules_sim.sample_for(rule)
    result = await asyncio.to_thread(tools.run_rule_action, rule, key, action, sample)
    from cygnus.rules import steps as rules_steps
    return JSONResponse({"ok": not result.startswith("[Error]"), "result": result,
                         "url": rules_engine.build_action_url(action, sample),
                         # A steps rule may stop on a question ("Restart Brave?",
                         # "Press play?"); the panel answers it at /apps/rules/test/answer.
                         "question": rules_steps.active()})


@router.post("/apps/rules/test/answer")
async def post_apps_rules_test_answer(message: Request):
    """Answer the question a tested steps rule is waiting on (yes / no).

    Body: {"app_key": "brave", "answer": "yes"}. Until now only a spoken reply
    reached the waiting run, so the panel's Test stopped at "Say yes, or no".
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    answer = (body.get("answer") or "").strip()
    if not answer:
        return JSONResponse({"error": "missing answer"}, status_code=400)
    from cygnus.rules import steps as rules_steps
    if not rules_steps.active():
        return JSONResponse({"error": "nothing is waiting for an answer"}, status_code=409)
    result = await asyncio.to_thread(rules_steps.resume, answer)
    if result is None:
        result = "Okay, I'll leave it there."
    return JSONResponse({"ok": not result.startswith("[Error]"), "result": result,
                         "question": rules_steps.active()})


def _apps_job(progress_key, work):
    """Run a slow Apps job (scan, AI sorting, launching an app) with progress
    published under progress_key for the panel's progress bar."""
    from cygnus.rules import sim as rules_sim
    try:
        return work(rules_sim.reporter(progress_key))
    finally:
        rules_sim.finish_progress(progress_key)


async def _json_body(message):
    try:
        return await message.json()
    except Exception:
        return None


@router.post("/apps/add")
async def post_apps_add(message: Request):
    """Add one app by name or program path: register it, work out what it is,
    create its predefined rules, and deep-scan it (opening it once)."""
    body = await _json_body(message)
    query = ((body or {}).get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "Type an app name or its program path."}, status_code=400)
    from cygnus.apps import abilities as app_abilities
    out = await asyncio.to_thread(
        _apps_job, "apps", lambda say: app_abilities.add_app(query, progress=say))
    return JSONResponse(dict(out, ok="error" not in out), status_code=400 if "error" in out else 200)


@router.post("/apps/fullscan")
async def post_apps_fullscan(message: Request):
    """Find every launchable app on this computer, hide noise, register the rest.
    Keeps every rule and choice the user already made."""
    from cygnus.apps import abilities as app_abilities
    out = await asyncio.to_thread(
        _apps_job, "apps", lambda say: app_abilities.full_scan(progress=say))
    return JSONResponse(dict(out, ok=True))


@router.post("/apps/abilities/generate")
async def post_apps_abilities_generate(message: Request):
    """Create predefined rules for all registered apps (or body.keys), without
    launching anything."""
    body = await _json_body(message) or {}
    keys = body.get("keys") if isinstance(body.get("keys"), list) else None
    from cygnus.apps import abilities as app_abilities
    out = await asyncio.to_thread(
        _apps_job, "apps", lambda say: app_abilities.generate(keys=keys, progress=say))
    return JSONResponse(dict(out, ok=True))


@router.post("/apps/abilities/scan")
async def post_apps_abilities_scan(message: Request):
    """Deep-scan one app's window ("Scan this app"). launch=true opens it if
    needed and closes it again afterwards."""
    body = await _json_body(message) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    from cygnus.apps import abilities as app_abilities
    launch = bool(body.get("launch", True))
    out = await asyncio.to_thread(
        _apps_job, key, lambda say: app_abilities.scan_app(key, launch=launch, progress=say))
    return JSONResponse(dict(out, ok="error" not in out), status_code=400 if "error" in out else 200)


@router.post("/apps/abilities/update")
async def post_apps_abilities_update(message: Request):
    """The user's on/off switch and consent level for one ability."""
    body = await _json_body(message) or {}
    from cygnus.apps import abilities as app_abilities
    ability = app_abilities.set_choice((body.get("key") or "").strip(),
                                       (body.get("id") or "").strip(),
                                       enabled=body.get("enabled"), level=body.get("level"))
    if ability is None:
        return JSONResponse({"error": "unknown ability"}, status_code=404)
    return JSONResponse({"ok": True, "ability": ability})


@router.post("/apps/abilities/test")
async def post_apps_abilities_test(message: Request):
    """Run one ability for real from the panel. `value` fills the ability's
    one needed detail (a website, a folder, a search), when it has one."""
    body = await _json_body(message) or {}
    key, aid = (body.get("key") or "").strip(), (body.get("id") or "").strip()
    from cygnus.apps import abilities as app_abilities
    entry = app_abilities.load_apps()["apps"].get(key) or {}
    ability = next((a for a in entry.get("abilities") or [] if a.get("id") == aid), None)
    if ability is None:
        return JSONResponse({"error": "unknown ability"}, status_code=404)
    args = {}
    needs = list((ability.get("needs") or {}).keys())
    if needs and str(body.get("value") or "").strip():
        args[needs[0]] = str(body["value"]).strip()[:300]
    # The owner clicked Test in the Apps panel: that is the confirm.
    result = await asyncio.to_thread(app_abilities.run_ability, key, aid, args, True)
    return JSONResponse({"ok": not result.startswith("[Error]"), "result": result})


@router.post("/apps/abilities/scan-all")
async def post_apps_scan_all(message: Request):
    """Start "Deep scan all" in the background: open, read and close each
    registered app not scanned yet, one at a time. Returns at once; progress
    is under key "apps-scan-all"."""
    import threading
    from cygnus.apps import abilities as app_abilities
    if not app_abilities.claim_scan_all():
        return JSONResponse({"error": "A deep scan of all apps is already running."}, status_code=409)
    total = len(app_abilities._scan_all_targets())
    threading.Thread(target=lambda: _apps_job(
        "apps-scan-all", lambda say: app_abilities.deep_scan_all(progress=say, claimed=True)),
        daemon=True).start()
    return JSONResponse({"ok": True, "started": True, "total": total})


@router.post("/apps/abilities/scan-all/cancel")
async def post_apps_scan_all_cancel(message: Request):
    """Stop after the app being scanned now; the next run resumes from there."""
    from cygnus.apps import abilities as app_abilities
    app_abilities.cancel_scan_all()
    return JSONResponse({"ok": True})


@router.get("/apps/abilities/scan-all")
async def get_apps_scan_all():
    from cygnus.apps import abilities as app_abilities
    return JSONResponse(dict(app_abilities.scan_all_status(), ok=True))


@router.get("/shadow/review")
async def get_shadow_review(all: int = 0):
    """Shadow mode: where the understand-first router would have acted
    differently from JARVIS, newest first, with readiness stats."""
    from cygnus.brain import intent_router
    return JSONResponse({"ok": True, "items": intent_router.review(disagreements_only=not all),
                         "stats": intent_router.stats()})


@router.post("/shadow/label")
async def post_shadow_label(message: Request):
    """The user's verdict on one shadow turn: "old", "new" or "neither"."""
    body = await _json_body(message) or {}
    from cygnus.brain import intent_router
    try:
        intent_router.label(str(body.get("id") or ""), str(body.get("verdict") or ""))
    except ValueError:
        return JSONResponse({"error": "verdict must be old, new or neither"}, status_code=400)
    return JSONResponse({"ok": True, "stats": intent_router.stats()})


@router.get("/prefs")
async def get_prefs():
    """Defaults JARVIS learned from use and you agreed to."""
    from cygnus.memory import preferences
    return JSONResponse({"ok": True, "defaults": preferences.all_defaults()})


@router.post("/prefs/forget")
async def post_prefs_forget(message: Request):
    body = await _json_body(message) or {}
    from cygnus.memory import preferences
    if not preferences.forget(str(body.get("topic") or "")):
        return JSONResponse({"error": "unknown default"}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/apps/rules/progress")
async def get_apps_rules_progress(key: str = ""):
    """What rule setup is doing right now, for the panel's progress bar:
    {"stage": "Checking https://...", "pct": 70, "done": false}."""
    from cygnus.rules import sim as rules_sim
    return JSONResponse(dict(rules_sim.get_progress((key or "").strip()), ok=True))


@router.post("/apps/rules/recheck")
async def post_apps_rules_recheck(message: Request):
    """Simulate a saved rule again in a hidden browser and save what it finds.

    Body: {"key": "brave", "rule_id": "search_a_movie"}. A broken search
    address is replaced by a working one when the check finds it (the site's
    own search box, a common pattern, or the AI's repair).
    """
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or body.get("app_key") or "").strip()
    rid = (body.get("rule_id") or "").strip()
    if not key or not rid:
        return JSONResponse({"error": "missing key or rule_id"}, status_code=400)
    from cygnus.rules import compiler as rules_compiler
    from cygnus.rules import engine as rules_engine
    from cygnus.rules import sim as rules_sim
    from cygnus.rules import voice as rules_voice
    from cygnus.apps.capabilities import REGISTRY_PATH
    entry = rules_voice._app_entry(key)
    rule = next((r for r in entry.get("compiled_rules") or [] if r.get("rule_id") == rid), None)
    if not rule:
        return JSONResponse({"error": "unknown rule_id"}, status_code=404)
    rule = dict(rule)
    if not isinstance(rule.get("action"), dict):
        # An older plain rule that names a site gets its derived action saved.
        derived = rules_engine.action_of(rule, key, entry)
        if derived:
            rule["action"] = derived
            rule["enforcement"] = "action"

    def _run():
        say = rules_sim.reporter(key)
        try:
            say("Re-checking your rule...", 10)
            return rules_sim.simulate(key, rule, entry, progress=say,
                                      user_text=rule.get("action_text")
                                      or rule.get("intent") or rule.get("source_phrase", ""))
        finally:
            rules_sim.finish_progress(key)

    new_rule, report = await asyncio.to_thread(_run)
    rules_compiler.commit_rules(key, [new_rule], REGISTRY_PATH, accept=True, merge=True)
    return JSONResponse({"ok": True, "key": key, "status": report["status"],
                         "steps": report["steps"], "judge": report["judge"],
                         "rule": new_rule})


@router.get("/apps/rules/list")
async def get_apps_rules_list(key: str = ""):
    """Phase 4: the rules currently committed for an app, human-readable."""
    key = (key or "").strip()
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    from cygnus.rules import voice as rules_voice
    out = rules_voice.list_rules(key)
    if out.get("status") == "error":
        return JSONResponse(out, status_code=400)
    return JSONResponse(dict(out, ok=True))


@router.get("/apps/registry")
async def get_apps_registry():
    """Full registry with registered/enabled/hidden flags, grouped for the
    Add-or-Remove-Programs-style panel. Returns:
    - installed (registered + enabled)
    - available (detected, not registered)
    - hidden (explicitly hidden by user)
    """
    from cygnus.apps.capabilities import load_registry
    from cygnus.apps.curate import registered_apps, detected_but_unregistered, is_registered, is_enabled
    reg = load_registry()
    if not reg:
        return JSONResponse({"error": "no registry", "installed": [], "available": [], "hidden": [], "count": 0})
    apps = reg.get("apps", {})
    installed = []
    available = []
    hidden = []
    for key, entry in apps.items():
        if not isinstance(entry, dict):
            continue
        binp = entry.get("bin") or ""
        item = {
            "name": entry.get("name", key),
            "key": key,
            "bin": binp,
            "category": entry.get("category", "other"),
            "kind": entry.get("kind", "gui"),
            "broken": bool(binp) and not os.path.exists(binp),
            "enabled": entry.get("enabled", True),
            "hidden": entry.get("hidden", False),
            "confidence": entry.get("confidence", "medium"),
            "rule_drafts": entry.get("rule_drafts") or [],
            "compiled_rules": entry.get("compiled_rules") or [],
        }
        if entry.get("hidden", False):
            hidden.append(item)
        elif is_enabled(key):
            installed.append(item)
        elif is_registered(key):
            installed.append(item)
        else:
            available.append(item)
    installed.sort(key=lambda a: (a["category"], a["name"]))
    available.sort(key=lambda a: (a["category"], a["name"]))
    hidden.sort(key=lambda a: (a["category"], a["name"]))
    return JSONResponse({
        "count": len(installed) + len(available) + len(hidden),
        "installed": installed,
        "available": available,
        "hidden": hidden,
        "generated": reg.get("generated"),
    })


@router.post("/apps/hide")
async def post_apps_hide(message: Request):
    """Hide an app from the registry (won't show in installed or available).
    The user can un-hide it later via the panel."""
    try:
        body = await message.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    key = (body.get("key") or "").strip()
    # `hidden` is the documented field; `hide` stays accepted for older callers.
    hide = bool(body.get("hidden", body.get("hide", True)))
    if not key:
        return JSONResponse({"error": "missing key"}, status_code=400)
    from cygnus.apps.capabilities import REGISTRY_PATH
    if not os.path.exists(REGISTRY_PATH):
        return JSONResponse({"error": "no registry"}, status_code=400)
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    apps = data.setdefault("apps", {})
    if key not in apps:
        return JSONResponse({"error": "unknown app"}, status_code=400)
    apps[key]["hidden"] = bool(hide)
    apps[key]["hidden_by"] = "user"      # a Full scan leaves this choice alone
    if hide and apps[key].get("registered"):
        apps[key]["registered"] = False  # unregistering on hide
        apps[key]["pinned_by"] = "user"
    tmp = REGISTRY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, REGISTRY_PATH)
    return JSONResponse({"ok": True, "key": key, "hidden": hide})


@router.get("/apps.html")
async def get_apps_panel():
    html_path = str(paths.APPS_PANEL_HTML)
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return _no_cache(HTMLResponse(content=html, status_code=200))


# Cached set of website keys (populated at module load so /apps/open can route
# websites to open_site() without first hitting GET /apps).
_WEBSITE_KEYS = frozenset()
try:
    _wi, _ = _website_items()
    _WEBSITE_KEYS = frozenset(i["key"] for i in _wi)
except Exception:
    pass
# (In server/app.py this ran before _website_items was defined, so the cache
# was always empty until the first GET /apps; here it fills at import.)
