"""从任务的事件流里算指标：全部来自服务端发出的事件，评测端不猜。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

Event = dict[str, Any]


def parse_sse(text: str) -> list[Event]:
    """解析 SSE 文本为事件字典列表（心跳与无法解析的帧跳过）。"""
    events: list[Event] = []
    for block in text.split("\n\n"):
        name, data = "", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if not name or name == "heartbeat" or not data:
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if isinstance(payload, dict):
            events.append({**payload, "event": name})
    return events


@dataclass(slots=True)
class CaseMetrics:
    #: 模型回复的条数（回合数）
    turns: int = 0
    llm_calls: int = 0
    llm_calls_by_node: dict[str, int] = field(default_factory=dict[str, int])
    tokens_by_node: dict[str, int] = field(default_factory=dict[str, int])
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    llm_ms: int = 0
    llm_retries: int = 0
    llm_errors: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    approvals: int = 0
    degraded_outputs: int = 0
    loop_blocked: int = 0
    error_notices: int = 0

    @property
    def cache_hit_rate(self) -> float | None:
        seen = self.cache_hit_tokens + self.cache_miss_tokens
        return self.cache_hit_tokens / seen if seen else None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["cache_hit_rate"] = self.cache_hit_rate
        return out


def summarize_events(events: list[Event]) -> CaseMetrics:
    m = CaseMetrics()
    for event in events:
        name = event.get("event")
        if name == "model.reply":
            m.turns += 1
        elif name == "llm.call":
            node = str(event.get("node", "?"))
            m.llm_calls += 1
            m.llm_calls_by_node[node] = m.llm_calls_by_node.get(node, 0) + 1
            m.tokens_by_node[node] = m.tokens_by_node.get(node, 0) + int(event.get("tokens", 0))
            m.prompt_tokens += int(event.get("prompt_tokens", 0))
            m.completion_tokens += int(event.get("completion_tokens", 0))
            m.reasoning_tokens += int(event.get("reasoning_tokens", 0))
            m.cache_hit_tokens += int(event.get("cache_hit_tokens", 0))
            m.cache_miss_tokens += int(event.get("cache_miss_tokens", 0))
            m.llm_ms += int(event.get("duration_ms", 0))
            m.llm_retries += max(0, int(event.get("attempts", 1)) - 1)
            m.llm_errors += 1 if event.get("outcome") not in (None, "ok") else 0
        elif name == "tool.call":
            m.tool_calls += 1
        elif name == "tool.observed" and event.get("ok") is False:
            m.tool_failures += 1
        elif name == "task.waiting_for_approval":
            m.approvals += 1
        elif name == "structured_output.degraded":
            m.degraded_outputs += 1
        elif name == "guard.loop_blocked":
            m.loop_blocked += 1
        elif name == "guard.error_notice":
            m.error_notices += 1
    return m


def final_event(events: list[Event]) -> Event:
    for event in reversed(events):
        if event.get("event") == "task.finished":
            return event
    return {}
