"""观察值治理：超限输出裁成摘要、全文落盘、摘要里带取回全文的句柄。

设计文档：docs/03_context/01_observation_governance.md。计数函数由调用方注入，本模块不依赖分词库。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from loguru import logger

_KEYWORDS: Final = re.compile(
    r"error|fatal|warning|failed|undefined reference|segmentation fault|traceback|exception|assert",
    re.IGNORECASE,
)
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]")
_MIN_LINE_CAP: Final = 80
_MAX_OBJECT_KEYS: Final = 20


@dataclass(frozen=True, slots=True)
class ObservationLimits:
    max_tokens: int
    head_lines: int
    tail_lines: int
    keyword_lines_limit: int
    json_outline_max_depth: int
    json_outline_array_items: int
    scalar_preview_chars: int
    exempt_tools: tuple[str, ...]
    exempt_multiplier: int
    #: 读类工具：自己有行数与翻页上限，不按头尾截断，只受 ``passthrough_max_tokens`` 约束
    passthrough_tools: tuple[str, ...] = ()
    passthrough_max_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Governed:
    #: 放进上下文的那一份
    summary: str
    truncated: bool
    artifact_id: str | None = None
    artifact_path: str | None = None


def _safe(name: str) -> str:
    return _UNSAFE.sub("_", name)


class ObservationStore:
    """全文落盘：``{root}/{task_id}/{artifact_id}.log``。失败不抛，返回 None。"""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def save(self, task_id: str, artifact_id: str, text: str) -> str | None:
        path = self._root / _safe(task_id) / f"{artifact_id}.log"
        try:
            await asyncio.to_thread(self._write, path, text)
        except OSError as exc:
            logger.error("观察值全文落盘失败（只保留摘要）：{}", exc)
            return None
        return str(path)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class ObservationGovernor:
    def __init__(
        self,
        limits: ObservationLimits,
        count_tokens: Callable[[str], int],
        store: ObservationStore,
    ) -> None:
        self._limits = limits
        self._count = count_tokens
        self._store = store

    async def govern(
        self,
        text: str,
        *,
        task_id: str,
        step: int,
        tool_name: str,
        taken: Collection[str],
    ) -> Governed:
        """``taken``：本任务已登记的产物 id，用来让新句柄不重名。"""
        limit = self._limit_for(tool_name)
        if self._count(text) <= limit:
            return Governed(text, False)  # 未超阈值：原样，没有磁盘副作用

        summary = self._outline_json(text) or self._distill_text(text, limit)
        artifact_id = _unique(f"obs_step_{step}_{_safe(tool_name)}", taken)
        path = await self._store.save(task_id, artifact_id, text)
        if path is None:
            return Governed(summary, True)
        handle = f"\n[完整输出已离线保存: artifact://{artifact_id}]"
        return Governed(summary + handle, True, artifact_id, path)

    def _limit_for(self, tool_name: str) -> int:
        limits = self._limits
        if tool_name in limits.passthrough_tools:
            return max(limits.passthrough_max_tokens, limits.max_tokens)
        exempt = tool_name in limits.exempt_tools
        return limits.max_tokens * (limits.exempt_multiplier if exempt else 1)

    # ------------------------------------------------------------------ JSON

    def _outline_json(self, text: str) -> str | None:
        stripped = text.strip()
        if not stripped.startswith(("{", "[")):
            return None
        try:
            value = json.loads(stripped)
        except (ValueError, RecursionError):
            return None  # 看上去像 JSON 但不是：按文本处理
        return json.dumps(self._outline(value, 0), ensure_ascii=False, indent=2)

    def _outline(self, value: Any, depth: int) -> Any:
        limits = self._limits
        if isinstance(value, str):
            preview = limits.scalar_preview_chars
            return value if len(value) <= preview else value[:preview] + "…"
        if isinstance(value, dict):
            if depth >= limits.json_outline_max_depth:
                return f"<object keys={list(value)[:10]}>"  # type: ignore[arg-type]
            items = list(value.items())  # type: ignore[arg-type]
            shown = {str(k): self._outline(v, depth + 1) for k, v in items[:_MAX_OBJECT_KEYS]}
            if len(items) > _MAX_OBJECT_KEYS:
                shown["…"] = f"其余 {len(items) - _MAX_OBJECT_KEYS} 个键省略"
            return shown
        if isinstance(value, list):
            if depth >= limits.json_outline_max_depth:
                return f"<list len={len(value)}>"  # type: ignore[arg-type]
            keep = limits.json_outline_array_items
            outlined = [self._outline(v, depth + 1) for v in value[:keep]]  # type: ignore[arg-type]
            if len(value) > keep:  # type: ignore[arg-type]
                outlined.append(f"…其余 {len(value) - keep} 项省略")  # type: ignore[arg-type]
            return outlined
        return value

    # ------------------------------------------------------------------ 文本

    def _distill_text(self, text: str, limit: int) -> str:
        limits = self._limits
        lines = text.splitlines() or [text]
        head, tail = limits.head_lines, limits.tail_lines
        if len(lines) > head + tail:
            middle = lines[head:-tail]
            keywords = [ln for ln in middle if _KEYWORDS.search(ln)][: limits.keyword_lines_limit]
            note = f"…中间省略 {len(middle)} 行"
            if keywords:
                note += f"，以下是其中的 {len(keywords)} 条关键行"
            lines = [*lines[:head], note, *keywords, *lines[-tail:]]
        # 行数少但每行巨长（如 base64）时，行数上的裁剪不起作用：再按长度封顶
        cap = max(_MIN_LINE_CAP, limit // len(lines))
        return "\n".join(ln if len(ln) <= cap else ln[:cap] + "…（本行已截断）" for ln in lines)


def _unique(base: str, taken: Collection[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"
