"""上下文工程：观察值治理、提示词装配、项目规则与提示词库。窗口与压缩、记忆待加入。

设计文档：docs/03_context/
"""

from agent_runtime.context.fold import FoldPolicy, FoldResult, fold_stream
from agent_runtime.context.observation import (
    Governed,
    ObservationGovernor,
    ObservationLimits,
    ObservationStore,
)
from agent_runtime.context.prompt import (
    CANARY_INSTRUCTION,
    MemoryEntry,
    ProjectRules,
    PromptAssembler,
    PromptAssets,
)
from agent_runtime.context.rules import PromptLibrary, RulesCache, RulesResult
from agent_runtime.context.task_compaction import (
    Compaction,
    CompactStep,
    TaskCompactionConfig,
    TaskCompactor,
    TaskSummary,
)

__all__ = [
    "CANARY_INSTRUCTION",
    "CompactStep",
    "Compaction",
    "FoldPolicy",
    "FoldResult",
    "Governed",
    "MemoryEntry",
    "ObservationGovernor",
    "ObservationLimits",
    "ObservationStore",
    "ProjectRules",
    "PromptAssembler",
    "PromptAssets",
    "PromptLibrary",
    "RulesCache",
    "RulesResult",
    "TaskCompactionConfig",
    "TaskCompactor",
    "TaskSummary",
    "fold_stream",
]
