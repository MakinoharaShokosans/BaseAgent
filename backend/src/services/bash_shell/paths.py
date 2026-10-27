"""写入目标越界校验。设计文档：docs/05_security/02_shell_sandbox.md §3.2。

只看**显式**写入目标（重定向、``tee``、``dd of=``）加调用方声明的路径；这是已知的局限，
不是完整的文件系统隔离。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Final

_SAFE_DEVICES: Final = frozenset(
    {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero", "/dev/tty"}
)
_REDIRECT: Final = re.compile(r"(?:\d?>>?|&>>?)\s*([^\s;&|<>]+)")
_DD_OF: Final = re.compile(r"\bof=([^\s;&|<>]+)")
_TEE: Final = re.compile(r"\btee\b((?:\s+(?:-[a-zA-Z]+|[^\s;&|<>-][^\s;&|<>]*))+)")


class PathEscape(ValueError):
    def __init__(self, target: str) -> None:
        super().__init__(f"写入目标 {target} 不在工作区内")
        self.target = target


def extract_write_targets(command: str) -> list[str]:
    targets: list[str] = [m.group(1) for m in _REDIRECT.finditer(command)]
    targets += [m.group(1) for m in _DD_OF.finditer(command)]
    for match in _TEE.finditer(command):
        try:
            parts = shlex.split(match.group(1))
        except ValueError:
            parts = match.group(1).split()
        targets += [p for p in parts if not p.startswith("-")]
    cleaned = [t.strip("'\"") for t in targets]
    return [t for t in cleaned if t and t[0] not in "&$" and t not in _SAFE_DEVICES]


def check_write_targets(root: Path, targets: Iterable[str]) -> None:
    base = root.resolve()
    for raw in targets:
        if "\x00" in raw:
            raise PathEscape(raw)
        candidate = Path(raw).expanduser()
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if resolved != base and not resolved.is_relative_to(base):
            raise PathEscape(raw)
