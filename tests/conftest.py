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
    import rules_sim
    import rules_steps
    import rules_voice
    monkeypatch.setattr(rules_sim, "PROBE", OfflineProbe())
    monkeypatch.setattr(rules_steps, "_RUN", {})
    monkeypatch.setattr(rules_steps, "_LAST", {})
    # module-level state must not leak between tests that reuse "brave"
    monkeypatch.setattr(rules_sim, "_PROGRESS", {})
    monkeypatch.setattr(rules_voice, "_PENDING", {})
    import tools
    tools._PENDING_RULE_SLOT.clear()
    # Learned defaults and the conversation state are per test, never the
    # user's real memory/preferences.json.
    import dialogue_state
    import preferences
    dialogue_state.reset()
    monkeypatch.setattr(preferences, "_PATH", str(tmp_path_factory.mktemp("prefs") / "p.json"))
    # The semantic router's model is ~0.5 GB; tests that need it fake it.
    import semantic_route
    monkeypatch.setattr(semantic_route, "MODE", "off")
    # open_application raises an app's open window instead of launching it;
    # tests see no running programs unless they fake some, and never move a
    # real window.
    import running_apps
    monkeypatch.setattr(running_apps, "inventory", lambda use_cache=True: [])
    monkeypatch.setattr(running_apps, "bring_to_front", lambda row: (False, "test: no real windows"))
    # 9router counts as up: no test may depend on (or start) the real one.
    # Tests of a router that is down fake it themselves.
    import router_keeper
    monkeypatch.setattr(router_keeper, "port_open", lambda timeout=0.5: True)
