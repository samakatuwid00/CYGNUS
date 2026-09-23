"""Reading the browser's history is opt-in.

On a fresh install the web registry is stale, so Cygnus scanned the default
browser's History database in the background at startup, without asking. For
the owner that is a feature; for anyone else it is a surprise, and it is
their browsing history (found while starting Cygnus as a stranger would,
2026-09-23)."""

import os


def test_the_setting_is_off_unless_asked_for(monkeypatch):
    # The reload re-reads the owner's .env, so the default is checked in the
    # source; the values are checked through the environment.
    src = open("cygnus/config.py", encoding="utf-8").read()
    assert 'os.getenv("JARVIS_SCAN_BROWSER_HISTORY", "0")' in src, "the default must be off"

    import importlib
    from cygnus import config
    for value, expected in (("0", False), ("", False), ("1", True)):
        monkeypatch.setenv("JARVIS_SCAN_BROWSER_HISTORY", value)
        assert importlib.reload(config).SCAN_BROWSER_HISTORY is expected


def test_the_server_checks_the_setting_before_scanning():
    src = open("cygnus/server/app.py", encoding="utf-8").read()
    before = src.split("maybe_refresh_async")[0]
    assert "SCAN_BROWSER_HISTORY" in before, \
        "startup must check the setting before it reads anyone's browser history"


def test_the_example_env_documents_it():
    assert "JARVIS_SCAN_BROWSER_HISTORY" in open(".env.example", encoding="utf-8").read()
