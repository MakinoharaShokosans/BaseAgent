"""记忆：存取、沉淀与会话压缩。设计文档：docs/03_context/04_memory.md、02_window_and_compaction.md。

只依赖 ``core`` 与 ``guardrails``；调用模型的提炼器由上层注入，所以这里的逻辑可以离线测试。
"""

from agent_runtime.memory.compactor import (
    CompactionConfig,
    CompactionResult,
    ExtractionRequest,
    Extractor,
    SessionCompactor,
)
from agent_runtime.memory.deposit import DepositResult, Outcome, deposit
from agent_runtime.memory.errors import (
    MemoryError_,
    SessionNotFound,
    WatermarkRegressionError,
    WorkspaceNotFound,
    WorkspacePathConflict,
)
from agent_runtime.memory.merge import (
    ExtractedFact,
    ExtractedFailure,
    Extraction,
    MergeLimits,
    MergeResult,
    Superseded,
    merge_extraction,
)
from agent_runtime.memory.models import (
    Fact,
    Failure,
    SessionMemory,
    Turn,
    Workspace,
    WorkspaceMemory,
)
from agent_runtime.memory.slicing import split_for_compaction
from agent_runtime.memory.store import MemoryStore

__all__ = [
    "CompactionConfig",
    "CompactionResult",
    "DepositResult",
    "ExtractedFact",
    "ExtractedFailure",
    "Extraction",
    "ExtractionRequest",
    "Extractor",
    "Fact",
    "Failure",
    "MemoryError_",
    "MemoryStore",
    "MergeLimits",
    "MergeResult",
    "Outcome",
    "SessionCompactor",
    "SessionMemory",
    "SessionNotFound",
    "Superseded",
    "Turn",
    "WatermarkRegressionError",
    "Workspace",
    "WorkspaceMemory",
    "WorkspaceNotFound",
    "WorkspacePathConflict",
    "deposit",
    "merge_extraction",
    "split_for_compaction",
]
