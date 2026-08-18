from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_deployment_docs_and_units_cover_api_storage_boundaries():
    public_docs = "\n".join(
        f"## {name}\n" + (ROOT / name).read_text(encoding="utf-8")
        for name in (
            "Website_deployment_plan.md",
            "README.md",
            "docs/DESIGN.md",
            "docs/OPENAI_API_STORAGE_IMPLEMENTATION_STATUS.md",
            "AGENTS.md",
            ".env.example",
            "deploy/systemd/paper-agent-cleanup.service",
            "deploy/systemd/paper-agent-cleanup.timer",
        )
    )
    for required in (
        "OPENAI_API_STORAGE_ROOT=/opt/paper-agent/data/openai_api",
        "paper-agent-cleanup.timer",
        "OnUnitActiveSec=1h",
        "2 vCPU / 4 GiB",
        "4 vCPU / 16 GiB",
        "/admin/api-storage",
        "75/85/95/98",
        "data/openai_api",
    ):
        assert required in public_docs
    assert "one Uvicorn worker" in public_docs or "--workers 1" in public_docs


def test_deployment_runbook_is_tracked_in_docs_and_release_gated():
    manual = (ROOT / "Website_deployment_plan.md").read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "/Website_deployment_plan.md" not in ignore
    assert "当前版本专属云服务器更新步骤" in manual
    assert "尚未获得用户对当前版本上云更新的明确确认" in manual
    assert "用户明确确认" in agents
    assert "需要随仓库提交到 GitHub" in agents
    assert "[Website_deployment_plan.md](Website_deployment_plan.md)" in readme


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
