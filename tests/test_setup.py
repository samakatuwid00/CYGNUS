"""The first-run setup: what the machine can run, trying a key, keeping it.

The key is the student's own and spends their quota, so: it is tried before
it is kept, it goes to Windows Credential Manager rather than .env, and the
endpoints that handle it answer only this computer's own pages.
"""

import sys
import types

import pytest

from cygnus.setup import machine, provider


@pytest.mark.parametrize("ram, gpus, local, whisper", [
    (3.8, [], "none", "tiny.en"),                                       # a 4 GB netbook
    (7.8, [{"name": "Intel UHD", "vram_gb": 0.1}], "backup", "base.en"),   # "8 GB"
    (15.2, [{"name": "AMD Radeon(TM) Graphics", "vram_gb": 0.5}], "backup", "small.en"),  # the author's
    (15.7, [{"name": "NVIDIA GeForce RTX 3060", "vram_gb": 6.0}], "main", "small.en"),
])
def test_the_machine_decides_what_is_offered(ram, gpus, local, whisper):
    got = machine.recommend({"ram_gb": ram, "gpus": gpus})
    assert (got["local"], got["whisper"]) == (local, whisper)
    assert got["cloud_first"] is (local != "main")
    # Measured 2026-09-30 (bench/providers.py): qwen3:1.7b picked the right
    # tool 4 of 8 times, qwen3:4b-instruct 6 of 8. Nothing smaller is offered.
    assert got["local_model"] in (None, "qwen3:4b-instruct")


def test_reading_this_machine_gives_numbers():
    m = machine.read()
    assert m["ram_gb"] > 0 and m["cores"] >= 1 and m["disk_free_gb"] >= 0
    assert isinstance(m["gpus"], list)


def _fake_openai(monkeypatch, reply=None, error=None):
    import openai

    class Client:
        def __init__(self, **kw):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

        def _create(self, **kw):
            if error:
                raise error
            return reply
    monkeypatch.setattr(openai, "OpenAI", Client)


def _tool_reply(called: bool):
    msg = types.SimpleNamespace(tool_calls=[object()] if called else None, content="ok")
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


def test_a_key_that_calls_a_tool_works(monkeypatch):
    _fake_openai(monkeypatch, reply=_tool_reply(True))
    got = provider.test("groq", "gsk-test")
    assert got["ok"] and "It works" in got["message"] and "gsk-test" not in str(got)


@pytest.mark.parametrize("error, works, says", [
    ("Error code: 401 - Incorrect API key provided", False, "refused this key"),
    ("Error code: 400 - Please pass a valid API key", False, "refused this key"),
    ("Error code: 429 - RESOURCE_EXHAUSTED", True, "free limit is used up"),
    ("Error code: 404 - model not found", False, "no model by that name"),
    ("APIConnectionError: Connection error.", False, "internet connection"),
])
def test_a_failed_try_says_what_to_do(monkeypatch, error, works, says):
    _fake_openai(monkeypatch, error=RuntimeError(error))
    got = provider.test("openai", "sk-test")
    assert got["ok"] is works and says in got["message"]


def test_a_model_without_tools_is_named(monkeypatch):
    _fake_openai(monkeypatch, reply=_tool_reply(False))
    got = provider.test("openrouter", "k", model="x")
    assert not got["ok"] and "supports tools" in got["message"]


def test_no_key_is_asked_for_before_any_call(monkeypatch):
    _fake_openai(monkeypatch, error=AssertionError("no call without a key"))
    assert provider.test("gemini", "  ")["message"] == "Paste the key first."


def test_saving_puts_the_key_in_the_vault_not_in_env(monkeypatch, tmp_path):
    from cygnus import paths
    vault = {}
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(
        set_password=lambda service, name, key: vault.__setitem__((service, name), key)))
    env = tmp_path / ".env"
    env.write_text("CYGNUS_API_KEY=old-key-in-the-file\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ENV_FILE", env)
    got = provider.save("gemini", "AIza-new", "gemini-2.5-flash")
    text = env.read_text(encoding="utf-8")
    assert vault == {("cygnus", "gemini"): "AIza-new"}
    assert "AIza-new" not in text and "old-key-in-the-file" not in text
    assert "CYGNUS_PROVIDER=gemini" in text and "CYGNUS_MODEL=gemini-2.5-flash" in text
    assert got["key_in"] == "Windows Credential Manager" and got["restart"]


def test_without_a_vault_the_key_goes_to_env(monkeypatch, tmp_path):
    from cygnus import paths

    def refuse(*a):
        raise RuntimeError("no backend")
    monkeypatch.setitem(sys.modules, "keyring", types.SimpleNamespace(set_password=refuse))
    monkeypatch.setattr(paths, "ENV_FILE", tmp_path / ".env")
    assert provider.save("groq", "gsk-x")["key_in"] == ".env"
    assert "CYGNUS_API_KEY=gsk-x" in (tmp_path / ".env").read_text(encoding="utf-8")


# ------------------------------------------------------------ the routes --

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from cygnus.server import app as server_app
    saved = []
    monkeypatch.setattr(provider, "save", lambda *a: saved.append(a) or {"ok": True})
    c = TestClient(server_app.app, base_url="http://127.0.0.1:8001")
    c.saved = saved
    return c


def test_the_setup_answers_this_computer(client):
    got = client.get("/setup/state").json()
    assert "gemini" in got["providers"] and got["recommend"]["whisper"].endswith(".en")


def test_the_phone_cannot_reach_the_setup(client):
    """tailscale serve adds X-Forwarded-For: the phone must not set the key."""
    assert client.get("/setup/state", headers={"X-Forwarded-For": "100.64.0.2"}).status_code == 403
    assert client.post("/setup/save", json={"provider": "gemini", "key": "k"},
                       headers={"X-Forwarded-For": "100.64.0.2"}).status_code == 403
    assert client.saved == []


def test_another_page_cannot_save_a_key(client):
    """A text/plain POST skips the CORS preflight; a foreign Origin is another site."""
    assert client.post("/setup/save", content='{"provider": "gemini", "key": "k"}',
                       headers={"Content-Type": "text/plain"}).status_code == 403
    assert client.post("/setup/save", json={"provider": "gemini", "key": "k"},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.saved == []
    assert client.post("/setup/save", json={"provider": "gemini", "key": "k"}).json() == {"ok": True}
    assert client.saved == [("gemini", "k", "", "")]
