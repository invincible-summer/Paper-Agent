"""Deployment units for API-only cleanup."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cleanup_service_is_root_limited_and_low_priority():
    text = (ROOT / "deploy/systemd/paper-agent-cleanup.service").read_text()
    assert "User=paper-agent" in text
    assert "ExecStart=/opt/paper-agent/.venv/bin/python" in text
    assert "--scheduled --reconcile" in text
    assert "ReadWritePaths=/opt/paper-agent/data/openai_api" in text
    assert "history_record" not in text
    assert "ReadWritePaths=/opt/paper-agent/data\n" not in text
    assert "UMask=0077" in text
    assert "IOSchedulingClass=idle" in text and "CPUWeight=20" in text


def test_cleanup_timer_matches_hourly_persistent_plan():
    text = (ROOT / "deploy/systemd/paper-agent-cleanup.timer").read_text()
    assert "OnBootSec=10min" in text
    assert "OnUnitActiveSec=1h" in text
    assert "Persistent=true" in text
    assert "RandomizedDelaySec=5min" in text
