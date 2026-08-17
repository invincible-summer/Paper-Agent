"""Production frontend must remain loopback-only behind nginx."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_start_script_binds_loopback():
    package = json.loads((ROOT / "frontend" / "package.json").read_text())
    start = package["scripts"]["start"]
    assert "-H 127.0.0.1" in start


def test_deployment_systemd_binds_nextjs_loopback_explicitly():
    manual = (ROOT / "Website_deployment_plan.md").read_text()
    assert "ExecStart=/usr/local/bin/pnpm exec next start -H 127.0.0.1 -p 3000" in manual
    assert "\nEnvironment=HOSTNAME=127.0.0.1\n" not in manual
