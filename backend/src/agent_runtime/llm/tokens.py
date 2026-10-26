"""用量估算：服务商不给用量时的兜底，宁高勿低。

不依赖分词器词表（tiktoken 需要预置缓存才能离线使用，见技术选型 §11 第 3 项）：
中日韩字符按一字一 token，其余按每 3 字符一 token（真实比例约 4，取小意味着估高）。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence

from agent_runtime.core import Message, ToolSchema

_WIDE = re.compile(r"[　-ヿ㐀-䶿一-鿿가-힯＀-￯]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    wide = len(_WIDE.findall(text))
    return wide + math.ceil((len(text) - wide) / 3)


def estimate_prompt_tokens(messages: Sequence[Message], tools: Sequence[ToolSchema] | None) -> int:
    total = 0
    for message in messages:
        total += 4 + estimate_tokens(message.content)  # 每条消息的固定开销
        for call in message.tool_calls:
            total += estimate_tokens(call.name) + estimate_tokens(
                json.dumps(call.args, ensure_ascii=False)
            )
    for tool in tools or ():
        total += estimate_tokens(
            json.dumps(
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
                ensure_ascii=False,
            )
        )
    return total


def estimate_completion_tokens(message: Message) -> int:
    total = estimate_tokens(message.content) + estimate_tokens(message.reasoning or "")
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(
            json.dumps(call.args, ensure_ascii=False)
        )
    return total
