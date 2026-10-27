"""输出蒸馏：流式、有界内存。设计文档：docs/05_security/02_shell_sandbox.md §3.5。

每个流只保留头部行、尾部行（双端队列）、关键字命中行与一块有上限的“结构化嗅探”缓冲；
完整输出由调用方写盘，不驻留内存。
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Final

import yaml

_KEYWORDS: Final = re.compile(
    r"error:|fatal:|warning:|undefined reference|segmentation fault|traceback|\bFAILED\b",
    re.IGNORECASE,
)
_LINE_CAP: Final = 2000


@dataclass(frozen=True, slots=True)
class DistillLimits:
    head_lines: int
    tail_lines: int
    max_keyword_lines: int
    sniff_max_bytes: int


@dataclass(frozen=True, slots=True)
class Distilled:
    text: str
    truncated: bool


@dataclass(slots=True)
class StreamDistiller:
    limits: DistillLimits
    log_hint: str = ""
    head: list[str] = field(default_factory=list[str])
    tail: deque[str] = field(default_factory=deque[str])
    keywords: list[str] = field(default_factory=list[str])
    total_lines: int = 0
    total_bytes: int = 0
    _sniff: bytearray = field(default_factory=bytearray)
    _sniff_overflow: bool = False
    _partial: str = ""

    def __post_init__(self) -> None:
        self.tail = deque(maxlen=self.limits.tail_lines)

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if not self._sniff_overflow:
            if len(self._sniff) + len(chunk) > self.limits.sniff_max_bytes:
                self._sniff_overflow = True
                self._sniff.clear()  # 大输出不会因为想看看是不是 JSON 而占住内存
            else:
                self._sniff += chunk
        text = self._partial + chunk.decode("utf-8", errors="replace")
        *lines, self._partial = text.split("\n")
        if len(self._partial) > _LINE_CAP * 4:  # 没有换行的巨长行：先吐出，别无限累积
            lines.append(self._partial)
            self._partial = ""
        for line in lines:
            self._line(line)

    def _line(self, line: str) -> None:
        line = line[:_LINE_CAP]
        self.total_lines += 1
        if len(self.head) < self.limits.head_lines:
            self.head.append(line)
        else:
            self.tail.append(line)
        if len(self.keywords) < self.limits.max_keyword_lines and _KEYWORDS.search(line):
            self.keywords.append(line)

    def finish(self, *, failed: bool) -> Distilled:
        if self._partial:
            self._line(self._partial)
            self._partial = ""
        if self.total_lines == 0:
            return Distilled("", False)
        structured = self._structured()
        if structured is not None:
            return Distilled(structured, False)
        omitted = self.total_lines - len(self.head) - len(self.tail)
        if failed and self.keywords:
            body = "\n".join(self.keywords)
            note = f"[仅显示关键字行 {len(self.keywords)} 条，共 {self.total_lines} 行]"
            return Distilled(f"{body}\n{note}{self._hint()}", True)
        if omitted <= 0:
            return Distilled("\n".join([*self.head, *self.tail]), False)
        note = f"... [省略 {omitted} 行]{self._hint()} ..."
        return Distilled("\n".join([*self.head, note, *self.tail]), True)

    def _hint(self) -> str:
        return f"，全文见 {self.log_hint}" if self.log_hint else ""

    def _structured(self) -> str | None:
        if self._sniff_overflow or not self._sniff or self._partial:
            return None
        raw = bytes(self._sniff).decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        try:
            if isinstance(json.loads(raw), dict | list):
                return raw
        except ValueError:
            pass
        try:
            if isinstance(yaml.safe_load(raw), dict | list):
                return raw
        except yaml.YAMLError:
            pass
        return None
