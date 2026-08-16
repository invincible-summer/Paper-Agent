"""Backend configuration loaded from project-root .env."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = str(_PROJECT_ROOT / ".env")

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Paper Agent"
    app_env: str = "local"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = True
    # Canonical public origin used in x_soda attachment URLs. Required in production.
    public_base_url: str = ""

    # LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model_light: str = "deepseek-v4-flash"
    deepseek_model_reasoning: str = "deepseek-v4-flash"

    # CORS / frontend
    # frontend_origin is the primary allowed origin. Allowed origins expand to
    # cover the common local/WSL hostnames + loopback IPs on the frontend port
    # so direct SSE calls (D-065) work from a browser without a wildcard policy.
    frontend_origin: str = "http://localhost:3000"
    # Comma-separated extra origins (e.g. a deployed domain). Empty by default.
    cors_origins: str = ""

    # Multi-user auth (frontend channel only; /v1 uses AGENT_API_KEY instead).
    # Off by default so local dev via start.sh keeps working with zero setup.
    auth_required: bool = False
    registration_open: bool = True
    guest_access: bool = True

    @property
    def allowed_origins(self) -> list[str]:
        """Explicit allow-list of CORS origins (D-089).

        Never returns "*" — the backend binds to 0.0.0.0 (LAN-reachable), so a
        wildcard origin with credentials would let any website on the user's
        network (or any origin, given 0.0.0.0) drive authenticated requests
        against /v1/chat/completions and drain the DeepSeek quota.

        Production (APP_ENV=production) stays explicit: frontend_origin +
        cors_origins only. In non-production (local dev, WSL2, LAN demos) the
        host's detected LAN IPv4s are also allowed on the frontend port, so a
        browser reaching the dev server via the machine IP (common on WSL2,
        where Windows reaches WSL through the WSL IP) doesn't hit a CORS 400.
        The egress-IP probe and ``hostname -I`` are best-effort and never raise.
        """
        from urllib.parse import urlparse

        extras = [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]
        base = [self.frontend_origin.rstrip("/")] if self.frontend_origin else []
        # Derive the frontend port and add localhost/loopback/WSL variants.
        parsed = urlparse(self.frontend_origin) if self.frontend_origin else None
        port = parsed.port if parsed and parsed.port else 3000
        variants = [
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
            # WSL2: Windows browser reaches the app via the WSL IP too.
            f"http://0.0.0.0:{port}",
        ]
        if self.app_env != "production":
            variants.extend(f"http://{ip}:{port}" for ip in _host_lan_ipv4s())
        # Dedup, preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for o in base + variants + extras:
            if o and o not in seen:
                seen.add(o)
                out.append(o)
        return out


def _host_lan_ipv4s() -> list[str]:
    """Best-effort non-loopback IPv4s a LAN/WSL2 browser might use to reach us.

    Two complementary probes (neither sends real traffic):
    - a UDP ``connect`` (no packets) reveals the default-egress interface IP;
    - ``hostname -I`` lists every NIC IP (Linux), widening coverage.
    Returns [] on any failure — dev convenience only, never raises.
    """
    import socket
    import subprocess

    ips: list[str] = []
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.connect(("8.8.8.8", 80))
            ips.append(sk.getsockname()[0])
        finally:
            sk.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=2)
        ips.extend(out.stdout.split())
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    result: list[str] = []
    for ip in ips:
        if ip and not ip.startswith("127.") and ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
