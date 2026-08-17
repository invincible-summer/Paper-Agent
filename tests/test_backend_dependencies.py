"""Packaging regressions for dependencies required during FastAPI startup."""

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_backend_declares_python_multipart_for_upload_routes():
    data = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text())
    dependencies = data["project"]["dependencies"]
    assert any(dep.split(";", 1)[0].strip().startswith("python-multipart")
               for dep in dependencies)


def test_constraints_pin_python_multipart():
    constraints = (ROOT / "constraints.txt").read_text()
    assert "python-multipart==0.0.32" in constraints
