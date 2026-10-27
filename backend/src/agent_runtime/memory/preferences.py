"""用户可调的设置：会话级（模型偏好、上下文上限）与全局（系统提示词覆盖）。

存储在记忆库的 ``session_settings`` / ``app_settings`` 两张表里；校验在 API 层做，
这里只负责容错地解析，读到坏数据时回落为缺省，不让一条坏设置拖垮任务。
设计文档：docs/09_api/01_endpoints.md §2.1。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Self

#: 全局设置的键
SYSTEM_PROMPT_KEY: Final = "system_prompt"
#: 会话上下文上限的下界：再小就连一轮完整的对话都放不下
MIN_CONTEXT_TOKEN_LIMIT: Final = 4000


@dataclass(frozen=True, slots=True)
class SessionPreferences:
    #: 首选端点名（``GET /models`` 的 ``name``）；``None`` 表示按配置顺序
    model: str | None = None
    #: 覆盖 ``[context.session].token_limit``；``None`` 表示用配置值
    context_token_limit: int | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Self:
        model = data.get("model")
        limit = data.get("context_token_limit")
        return cls(
            model=model if isinstance(model, str) and model else None,
            context_token_limit=(
                limit
                if isinstance(limit, int)
                and not isinstance(limit, bool)
                and limit >= MIN_CONTEXT_TOKEN_LIMIT
                else None
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"model": self.model, "context_token_limit": self.context_token_limit}
