"""The System Status panel should not show a column of em-dashes.

"STT" and "Vault index" were filled in the HUD's demo mode only, so on a real
page they never changed (2026-09-23). /status now answers both, and the rows
start at "checking..." so a slow page cannot be mistaken for a dead server.

Nothing here imports cygnus.server.app: importing it starts the real Hermes
warm session in a thread, which then overwrote another test's monkeypatched
session id.
"""

import re
from pathlib import Path

from cygnus.tools import vault

ROOT = Path(__file__).resolve().parents[1]
HUD = (ROOT / "web" / "jarvis_hud_v3.html").read_text(encoding="utf-8")
APP = (ROOT / "cygnus" / "server" / "app.py").read_text(encoding="utf-8")


def test_status_carries_the_vault_row():
    assert '"vault":' in APP and "index_state()" in APP


def test_index_state_says_none_without_a_vault(monkeypatch, tmp_path):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "nothing-here")
    assert vault.index_state() == "none"


def test_index_state_says_notes_only_without_an_index(monkeypatch, tmp_path):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    assert vault.index_state() == "notes only"


def test_index_state_reports_the_index_age(monkeypatch, tmp_path):
    (tmp_path / "turbovec_index.tv").write_bytes(b"x")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    assert vault.index_state() == "turbovec · today"


def test_the_hud_fills_stt_and_the_vault_row_outside_demo_mode():
    fill = HUD[HUD.index("function refreshServerStats"):]
    fill = fill[:fill.index("// ---------------------------------------------------------------- transcript")]
    assert "st-stt" in fill and "st-vault" in fill


def test_server_rows_start_as_checking_not_a_dash():
    for row in ("st-brain", "st-stt", "st-route", "st-vault", "st-wake"):
        m = re.search(r'id="%s">([^<]*)</span>' % row, HUD)
        assert m, row
        assert m.group(1) != "—", f"{row} still ships an em-dash placeholder"


def test_an_unreachable_server_marks_every_server_row():
    fail = HUD[HUD.index(".catch(function () {"):]
    fail = fail[:fail.index("syncBrainPill();")]
    for row in ("st-stt", "st-route", "st-vault", "st-wake"):
        assert row in fail, row
