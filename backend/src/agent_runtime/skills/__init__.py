"""技能注册表：来源信任、元数据治理、渐进式披露。

设计文档：docs/04_tools/05_skills.md
"""

from agent_runtime.skills.registry import (
    MANIFEST_NOTICE,
    SkillLimits,
    SkillRecord,
    SkillRegistry,
    SkillRoot,
    SkillSource,
    skill_roots,
)

__all__ = [
    "MANIFEST_NOTICE",
    "SkillLimits",
    "SkillRecord",
    "SkillRegistry",
    "SkillRoot",
    "SkillSource",
    "skill_roots",
]
