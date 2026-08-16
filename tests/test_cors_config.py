"""Tests for the backend CORS allow-list (D-089).

The backend binds to 0.0.0.0 (LAN-reachable). CORS must never be "*" with
credentials, otherwise any website can drive authenticated requests. These
tests pin the derived origin list so a regression to a wildcard is caught.
"""

"""Tests for the backend CORS allow-list (D-089).

The backend binds to 0.0.0.0 (LAN-reachable). CORS must never be "*" with
credentials, otherwise any website can drive authenticated requests. These
tests pin the derived origin list so a regression to a wildcard is caught.
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import Settings


def test_allowed_origins_never_wildcard():
    s = Settings()
    origins = s.allowed_origins
    assert "*" not in origins
    assert len(origins) >= 1
    joined = " ".join(origins)
    assert "localhost" in joined
    assert "127.0.0.1" in joined


def test_extra_origins_merged(monkeypatch):
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://paper.example.com")
    monkeypatch.setenv("CORS_ORIGINS", "https://paper.example.com, https://alt.example.com")
    s = Settings()
    origins = s.allowed_origins
    assert "https://paper.example.com" in origins
    assert "https://alt.example.com" in origins
    assert "*" not in origins


def test_dev_mode_includes_host_lan_ips(monkeypatch):
    """Non-production auto-allows the host's LAN/WSL2 IPs on the frontend port,
    so a browser reaching the dev server via the machine IP doesn't CORS-400."""
    import app.core.config as cfg
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setattr(cfg, "_host_lan_ipv4s", lambda: ["192.168.2.27", "10.0.0.5"])
    s = Settings()
    origins = s.allowed_origins
    assert "http://192.168.2.27:3000" in origins
    assert "http://10.0.0.5:3000" in origins
    assert "*" not in origins


def test_production_excludes_host_lan_ips(monkeypatch):
    """Production stays explicit — frontend_origin + cors_origins only, never
    auto-trusting LAN IPs (the backend must not widen trust on a server)."""
    import app.core.config as cfg
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(cfg, "_host_lan_ipv4s", lambda: ["192.168.2.27", "10.0.0.5"])
    s = Settings()
    origins = s.allowed_origins
    assert "http://192.168.2.27:3000" not in origins
    assert "http://10.0.0.5:3000" not in origins
    # The explicit base is still present.
    assert "http://localhost:3000" in origins
