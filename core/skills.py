"""Skill registry — Anthropic-style Agent Skills with progressive disclosure.

A skill is a directory under ``skills/builtin/`` containing a ``SKILL.md``:
YAML frontmatter (name, version, description, requires_papers) followed by
markdown instructions. Only name + one-line description go into the system
prompt (~50 tokens each); the full instructions load on demand through the
``use_skill`` tool, so per-turn prompt cost stays flat no matter how many
skills exist. Dispatch intelligence lives in the description: it must say
WHEN the skill triggers — that is the mainstream industrial practice.

Skill instruction text is registered in the prompt registry (``skill.<name>``),
so every turn's trace pins the exact skill version that produced an answer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.prompts.registry import register

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills" / "builtin"


@dataclass(frozen=True)
class SkillDef:
    name: str
    version: int
    description: str
    requires: tuple[str, ...]   # preconditions: papers / map / review / attachments
    output_check: str           # self-check contract injected with the instructions
    path: Path


_SKILLS: dict[str, SkillDef] = {}

_VALID_REQUIRES = {"papers", "map", "review", "attachments"}


def _parse_skill(md_path: Path) -> SkillDef | None:
    """Parse one SKILL.md; malformed files are skipped with a warning."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        logger.warning("skill %s: missing YAML frontmatter, skipped", md_path)
        return None
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        logger.warning("skill %s: bad frontmatter (%s), skipped", md_path, e)
        return None
    body = parts[2].strip()
    name = str(meta.get("name") or md_path.parent.name).strip()
    description = str(meta.get("description") or "").strip()
    try:
        version = int(meta.get("version") or 1)
    except (TypeError, ValueError):
        version = 1
    if not name or not description or not body:
        logger.warning("skill %s: name/description/body required, skipped", md_path)
        return None
    # requires: list form preferred; legacy `requires_papers: true` maps to ["papers"].
    requires = meta.get("requires")
    if requires is None:
        requires = ["papers"] if meta.get("requires_papers") else []
    if isinstance(requires, str):
        requires = [requires]
    requires = [str(r).strip() for r in requires if str(r).strip() in _VALID_REQUIRES]
    output_check = str(meta.get("output_check") or "").strip()
    register(f"skill.{name}", version, body)
    return SkillDef(name=name, version=version, description=description,
                    requires=tuple(requires), output_check=output_check, path=md_path)


def load_skills() -> dict[str, SkillDef]:
    """Scan skills/builtin and (re)load every SKILL.md. Idempotent."""
    _SKILLS.clear()
    if _SKILLS_DIR.is_dir():
        for md in sorted(_SKILLS_DIR.glob("*/SKILL.md")):
            sk = _parse_skill(md)
            if sk is not None:
                _SKILLS[sk.name] = sk
    return _SKILLS


def all_skills() -> list[SkillDef]:
    if not _SKILLS:
        load_skills()
    return list(_SKILLS.values())


def get_skill(name: str) -> SkillDef | None:
    if not _SKILLS:
        load_skills()
    return _SKILLS.get(name)


def skills_prompt_section() -> str:
    """Compact metadata list for the system prompt (progressive disclosure).

    Only name + one-line description; the full instructions load via the
    use_skill tool when the model judges a request matches the trigger scene.
    """
    skills = all_skills()
    if not skills:
        return ""
    lines = [
        "## 技能（按需加载）",
        "",
        "以下是封装好的工作流技能。用户请求命中某个技能的描述场景时，先调用 "
        "use_skill(name) 加载它的完整指令，再严格按指令组合基础工具执行；未命中任何技能时不要加载。",
        "",
    ]
    lines += [f"- {s.name} — {s.description}" for s in skills]
    return "\n".join(lines)
