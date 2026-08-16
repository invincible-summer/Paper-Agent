from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deployment_manual_documents_api_storage_and_capacity_boundaries():
    text = (ROOT / "Website_deployment_plan.md").read_text(encoding="utf-8")
    for required in (
        "OPENAI_API_STORAGE_ROOT=/opt/paper-agent/data/openai_api",
        "paper-agent-cleanup.timer", "OnUnitActiveSec=1h", "2 vCPU / 4 GiB",
        "4 vCPU / 16 GiB", "/admin/api-storage", "75/85/95/98",
        "不要长期备份", "data/openai_api/blobs",
    ):
        assert required in text
    assert "不要通过增加 Uvicorn worker" in text


def test_readme_design_and_agents_state_api_web_isolation():
    combined = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("README.md", "docs/DESIGN.md", "AGENTS.md")
    )
    assert "data/openai_api" in combined
    assert "完整 messages" in combined
    assert "API Trace" in combined
    assert "自制前端" in combined
    assert "/admin/api-storage" in combined


def test_env_example_has_placeholders_not_runtime_secret():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "# OPENAI_API_STORAGE_ROOT=/opt/paper-agent/data/openai_api" in text
    assert "# OPENAI_API_POLICY_PRESET=balanced" in text
