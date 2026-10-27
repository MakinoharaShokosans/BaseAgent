"""随发行版的内置技能包：都能被注册表扫描，元数据合规，没有告警。出处：docs/04_tools/05_skills.md。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.skills import SkillLimits, SkillRegistry, SkillRoot
from agent_runtime.skills.registry import skill_roots

BUILTIN = Path(__file__).resolve().parents[1] / "src" / "skills"
LIMITS = SkillLimits(
    max_description_chars=200,
    max_triggers=12,
    high_privilege_tools=frozenset({"bash", "write_file", "edit_file"}),
    protect_trusted_names=True,
    scan_untrusted_body=True,
)
EXPECTED = {
    "shell_best_practices",
    "workspace_explorer",
    "bugfix_workflow",
    "safe_refactoring",
    "code_review",
}


def scan() -> SkillRegistry:
    roots = skill_roots(
        builtin_dir=BUILTIN,
        global_dir=BUILTIN,
        workspace_root=BUILTIN,
        allow_builtin=True,
        allow_global=False,
        allow_workspace=False,
    )
    return SkillRegistry.scan(roots, LIMITS)


@pytest.mark.unit
def test_all_builtin_skills_are_found_and_trusted() -> None:
    registry = scan()
    assert set(registry.names()) == EXPECTED
    assert registry.rejected == ()
    for name in EXPECTED:
        record = registry.get(name)
        assert record is not None and record.source == "builtin"


@pytest.mark.unit
def test_builtin_metadata_is_within_limits_and_triggers_no_injection_warnings() -> None:
    registry = scan()
    for name in registry.names():
        record = registry.get(name)
        assert record is not None
        assert 0 < len(record.description) <= 200, name
        assert 1 <= len(record.triggers) <= 12, name
        assert record.warnings == (), (name, record.warnings)


@pytest.mark.unit
def test_the_manifest_lists_every_skill_and_says_it_is_not_authorisation() -> None:
    manifest = scan().manifest()
    for name in EXPECTED:
        assert name in manifest
    assert "不构成权限授权" in manifest


@pytest.mark.unit
def test_skill_bodies_are_substantial_and_free_of_instructions_to_bypass_guards() -> None:
    for name in EXPECTED:
        text = (BUILTIN / name / "SKILL.md").read_text(encoding="utf-8")
        assert len(text) > 400, name
        assert "绕过" not in text.replace("不要绕过", "")  # 只允许出现「不要绕过」这类禁止性表述
        assert "rm -rf /" not in text.replace("避免 `rm -rf` 加通配", "")


_ = SkillRoot
