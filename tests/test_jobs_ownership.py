"""Only the server process may call a running job "interrupted by restart".
Importing jarvis_web (a test, apps_panel_test.py, a delegated Claude's own
pytest run) must not claim the registry: on 2026-09-15 it did, and the
job that ran the test was marked interrupted while still running."""

import re


def test_importing_jarvis_web_does_not_claim_the_jobs_registry():
    src = open("cygnus/server/app.py", encoding="utf-8").read()
    body, _, main = src.partition("def main():")
    assert not re.search(r"^\s*\w*\.?claim_ownership\(\)", body, re.M), \
        "claim_ownership() at import time marks other processes' running jobs interrupted"
    assert "claim_ownership()" in main
