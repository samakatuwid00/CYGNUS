"""Installed by the desktop installer, Cygnus's code sits read-only in the
app's resources/backend - no pytest.ini, no desktop/ beside it - and its
settings and data live in the user's folder (desktop/main.js)."""

import importlib

from cygnus import paths


def test_the_settings_file_can_live_elsewhere(monkeypatch, tmp_path):
    monkeypatch.setenv("CYGNUS_ENV_FILE", str(tmp_path / "Cygnus" / ".env"))
    try:
        assert importlib.reload(paths).ENV_FILE == tmp_path / "Cygnus" / ".env"
    finally:
        monkeypatch.delenv("CYGNUS_ENV_FILE")
        importlib.reload(paths)


def test_the_root_is_found_in_an_installed_backend(monkeypatch, tmp_path):
    """resources/backend holds cygnus/ and web/ only."""
    backend = tmp_path / "resources" / "backend"
    (backend / "cygnus").mkdir(parents=True)
    (backend / "web").mkdir()
    (backend / "web" / "jarvis_hud_v3.html").write_text("<html>", encoding="utf-8")
    fake_file = backend / "cygnus" / "paths.py"
    fake_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "__file__", str(fake_file))
    assert paths._find_root() == backend


def test_the_repository_is_still_its_own_root():
    assert (paths.ROOT / "cygnus" / "paths.py").is_file()
    assert (paths.ROOT / "web" / "jarvis_hud_v3.html").is_file()
