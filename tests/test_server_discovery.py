"""Finding the running server, and the process behind it.

Both failed on 2026-09-23 while Cygnus was answering turns: a Laravel dev
server held 8001 so Cygnus came up on 8002 and every bench script said "no
Cygnus server", and soak.py's command-line match logged "server gone" for a
server that replied to the same check.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Conn:
    def __init__(self, port, pid=None, listen=True):
        self.laddr = type("A", (), {"port": port})()
        self.status = "LISTEN" if listen else "ESTABLISHED"
        self.pid = pid


class _Proc:
    def __init__(self, pid, cmdline, ports=(), name="python.exe", rss=50 << 20):
        self.pid = pid
        self.info = {"cmdline": cmdline.split(), "name": name}
        self._ports = ports
        self._rss = rss

    def net_connections(self, kind="inet"):
        return [_Conn(p, self.pid) for p in self._ports]

    def memory_info(self):
        return type("M", (), {"rss": self._rss})()


def _fake_psutil(procs, conns=()):
    class P:
        CONN_LISTEN = "LISTEN"
        Error = OSError

        @staticmethod
        def process_iter(attrs=None, ad_value=None):
            return list(procs)

        @staticmethod
        def net_connections(kind="inet"):
            if not conns:
                raise OSError("no permission")
            return list(conns)

        @staticmethod
        def Process(pid):
            return next(p for p in procs if p.pid == pid)
    return P


def test_a_server_on_an_unexpected_port_is_still_found(monkeypatch):
    client = _load("scripts/cygnus_client.py", "cygnus_client_under_test")
    fake = _fake_psutil([_Proc(1, r"python.exe -m cygnus", ports=(8002,))])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert "http://127.0.0.1:8002" in client.candidates()


def test_discovery_survives_no_psutil(monkeypatch):
    client = _load("scripts/cygnus_client.py", "cygnus_client_under_test")
    monkeypatch.setitem(sys.modules, "psutil", None)   # import psutil -> TypeError
    assert client.candidates()          # the fixed candidates are still there


def test_the_process_is_found_by_the_port_it_answers_on(monkeypatch):
    soak = _load("scripts/bench/soak.py", "soak_under_test")
    server = _Proc(42, r"python.exe -m cygnus", ports=(8002,))
    fake = _fake_psutil([server], conns=[_Conn(8002, 42)])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert soak.backend_process(8002).pid == 42


def test_a_wrapper_that_merely_mentions_cygnus_is_not_the_server(monkeypatch):
    """This script's own command line contains "-m cygnus"; it is not the server."""
    soak = _load("scripts/bench/soak.py", "soak_under_test")
    wrapper = _Proc(7, r"bash.exe -c python -m cygnus check", name="bash.exe",
                    rss=900 << 20)
    server = _Proc(42, r"python.exe -m cygnus", rss=70 << 20)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([wrapper, server]))
    assert soak.backend_process().pid == 42


@pytest.mark.parametrize("base, want", [
    ("http://127.0.0.1:8002", 8002),
    ("http://127.0.0.1:8001/", 8001),
    ("http://localhost", None),
])
def test_port_of(base, want):
    soak = _load("scripts/bench/soak.py", "soak_under_test")
    assert soak._port_of(base) == want
