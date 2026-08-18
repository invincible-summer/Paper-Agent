from pathlib import Path


def test_rxiv_systemd_units_are_bounded_and_data_only():
    root=Path(__file__).resolve().parents[1]
    service=(root/"deploy/systemd/paper-agent-rxiv-sync.service").read_text()
    timer=(root/"deploy/systemd/paper-agent-rxiv-sync.timer").read_text()
    assert "scripts/sync_rxiv_metadata.py --scheduled --max-seconds 870" in service
    assert "RuntimeMaxSec=15min" in service
    assert "ReadWritePaths=/opt/paper-agent/data" in service
    assert "EnvironmentFile=/opt/paper-agent/.env" in service
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
