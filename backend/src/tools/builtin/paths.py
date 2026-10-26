"""文件类工具共用的路径校验与参数取值。设计文档：04_tools/01_tool_system.md §11。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_runtime.core import CoreError


class PathEscapeError(CoreError):
    """路径不在工作区内，或根本不是一个可用的路径。"""


def resolve_within_root(root: Path, raw: str) -> Path:
    """相对路径按工作区根解释；``resolve()`` 展开符号链接与 ``..`` 后必须仍在根之内。

    目标不存在时也成立：``resolve`` 会展开存在的前缀，所以指向工作区外的符号链接目录
    之下的新文件同样被拦住。
    """
    if not raw.strip():
        raise PathEscapeError("路径不能为空")
    try:
        resolved = (root / raw).resolve()
    except (ValueError, OSError, RuntimeError) as exc:  # NUL 字节、符号链接环
        raise PathEscapeError(f"路径不可用：{exc}") from exc
    base = root.resolve()
    if resolved != base and not resolved.is_relative_to(base):
        raise PathEscapeError("路径必须位于工作区内")
    return resolved


def text_arg(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) else None


def int_arg(args: Mapping[str, Any], key: str) -> int | None:
    """缺省为 None；布尔与小数不算整数，抛 ``ValueError`` 让调用方转成失败观察值。"""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} 必须是整数")
    return value
