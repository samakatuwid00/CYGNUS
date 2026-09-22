"""Shared test setup: no test may open a real browser.

rules_sim checks rules by loading real pages in a hidden Chrome. Tests get an
offline page checker that answers "blocked" at once; tests that exercise the
simulation pass their own fake probe.
"""

import pytest


class OfflineProbe:
    def discover_search(self, site_url):
        return {"blocked": True, "title": "offline"}

    def probe_search(self, search_url, sample):
        return {"status": "blocked", "evidence": "offline test", "title": "",
                "url": search_url.replace("{query}", sample)}

    def probe_open(self, url):
        return {"status": "blocked", "evidence": "offline test", "title": "", "url": url}


@pytest.fixture(autouse=True)
def offline_probe(monkeypatch, tmp_path_factory):
    from cygnus.rules import sim as rules_sim
    from cygnus.rules import steps as rules_steps
    from cygnus.rules import voice as rules_voice
    monkeypatch.setattr(rules_sim, "PROBE", OfflineProbe())
    monkeypatch.setattr(rules_steps, "_RUN", {})
    monkeypatch.setattr(rules_steps, "_LAST", {})
    # module-level state must not leak between tests that reuse "brave"
    monkeypatch.setattr(rules_sim, "_PROGRESS", {})
    monkeypatch.setattr(rules_voice, "_PENDING", {})
    from cygnus import tools
    tools._PENDING_RULE_SLOT.clear()
    # Learned defaults and the conversation state are per test, never the
    # user's real memory/preferences.json.
    from cygnus.brain import dialogue_state
    from cygnus.memory import preferences
    dialogue_state.reset()
    monkeypatch.setattr(preferences, "_PATH", str(tmp_path_factory.mktemp("prefs") / "p.json"))
    # The semantic router's model is ~0.5 GB; tests that need it fake it.
    from cygnus.brain import semantic_route
    monkeypatch.setattr(semantic_route, "MODE", "off")
    # open_application raises an app's open window instead of launching it;
    # tests see no running programs unless they fake some, and never move a
    # real window.
    from cygnus.apps import running as running_apps
    monkeypatch.setattr(running_apps, "inventory", lambda use_cache=True: [])
    monkeypatch.setattr(running_apps, "bring_to_front", lambda row: (False, "test: no real windows"))
    # 9router counts as up: no test may depend on (or start) the real one.
    # Tests of a router that is down fake it themselves.
    from cygnus.brain import router_keeper
    monkeypatch.setattr(router_keeper, "port_open", lambda timeout=0.5: True)
    monkeypatch.setattr(router_keeper, "responds", lambda timeout=5.0: True)
    # Tests never write the owner's audit log: the smoke test and "what did
    # you do?" read logs/audit.jsonl, and test calls were landing in it
    # (found 2026-09-22). Tests that read an audit log set their own path.
    import pathlib
    audit_dir = pathlib.Path(tmp_path_factory.mktemp("audit"))
    monkeypatch.setattr(tools, "_AUDIT_PATH", audit_dir / "audit.jsonl")
    from cygnus import audit
    monkeypatch.setattr(audit, "AUDIT_DIR", str(audit_dir))
    monkeypatch.setattr(audit, "AUDIT_PATH", str(audit_dir / "audit.jsonl"))
    from cygnus.brain import intent_router
    monkeypatch.setattr(intent_router, "AUDIT_LOG", str(audit_dir / "audit.jsonl"))
