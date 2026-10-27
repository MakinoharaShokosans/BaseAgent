"""暴露给模型的工具名：``mcp__{服务器}__{工具}``，规范化为端点接受的 ``[a-zA-Z0-9_-]{1,N}``。

设计文档：docs/04_tools/04_mcp.md §5、§9.2。过长或规范化后撞名时附短哈希；映射由管理器维护，
调用时从暴露名反查（服务器，原始工具名），不做猜测性路由。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Final

PREFIX: Final = "mcp"
_ILLEGAL: Final = re.compile(r"[^A-Za-z0-9_-]")
_HASH_LEN: Final = 8


def _clean(part: str) -> str:
    return _ILLEGAL.sub("_", part)


def _digest(server: str, tool: str) -> str:
    return hashlib.sha1(f"{server}\0{tool}".encode()).hexdigest()[:_HASH_LEN]


def _with_hash(base: str, server: str, tool: str, max_len: int) -> str:
    keep = max(max_len - _HASH_LEN - 1, 1)
    return f"{base[:keep]}_{_digest(server, tool)}"


def assign_names(pairs: Iterable[tuple[str, str]], max_len: int) -> dict[tuple[str, str], str]:
    """为一批（服务器，工具）分配暴露名：确定性（按输入排序）、互不相同、长度不超过 ``max_len``。"""
    taken: set[str] = set()
    out: dict[tuple[str, str], str] = {}
    for server, tool in sorted(set(pairs)):
        base = f"{PREFIX}__{_clean(server)}__{_clean(tool)}"
        name = base if len(base) <= max_len else _with_hash(base, server, tool, max_len)
        if name in taken:  # 规范化后撞名（如 a.b 与 a_b）：带上原名的哈希区分
            name = _with_hash(base, server, tool, max_len)
        taken.add(name)
        out[(server, tool)] = name
    return out
