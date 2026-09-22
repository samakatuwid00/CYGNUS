"""healthy_models() and candidates() share router_health's lock; neither
may call the other while holding it (a deadlock hung the suite, 2026-09-22)."""

import threading

import router_health as rh


def test_healthy_models_returns_with_the_real_candidates(monkeypatch):
    monkeypatch.setattr(rh, "_state", {})
    rh.mark_healthy("ag/claude-sonnet-4-6", 1.0)
    out = []
    t = threading.Thread(target=lambda: out.append(rh.healthy_models()), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "healthy_models() deadlocked"
    assert "ag/claude-sonnet-4-6" in out[0]
