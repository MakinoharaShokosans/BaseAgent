"""权限判定（纯函数）。设计文档：docs/05_security/01_permission_tiers.md。

分类基于命令文本的正则匹配：确定、可测、可解释，但不是语义分析，是“防误操作”的闸门，
不是对抗恶意输入的安全边界（见该文档 §4）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from re import Pattern
from typing import Any, Final

from loguru import logger

#: 级别偏序：数值越大权限越高
LEVEL_ORDER: Final[Mapping[str, int]] = {
    "read_only": 0,
    "workspace_write": 1,
    "full_permissions": 2,
}
_DEFAULT_LEVEL: Final = "workspace_write"
_SUMMARY_LIMIT: Final = 200
_NETWORK_COMMAND: Final = re.compile(r"\b(git\s+push|curl|wget|ssh|scp|nc|telnet)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PermissionPolicy:
    """判定所需的配置快照（由 ``[permissions]`` 装配，见 ``runtime.assembly``）。"""

    read_only_tools: frozenset[str]
    workspace_write_tools: frozenset[str]
    full_permission_tools: frozenset[str]
    bash_tools: frozenset[str]
    full_permission_patterns: tuple[str, ...]
    workspace_write_patterns: tuple[str, ...]
    unknown_tool_level: str


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    required_level: str
    current_level: str
    #: 稳定枚举值，前端据此映射图标与文案，不要随意改名：
    #: read | workspace_write | network_egress | global_env | privileged | unknown
    action_type: str
    #: 给审批人的一句话
    action_summary: str
    reason: str


def normalize_level(level: object) -> str:
    text = str(level or "").strip().lower()
    return text if text in LEVEL_ORDER else _DEFAULT_LEVEL


@lru_cache(maxsize=256)
def _compile(pattern: str) -> Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        logger.error("忽略非法的权限正则 {!r}：{}", pattern, exc)
        return None


def _first_match(command: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        compiled = _compile(pattern)
        if compiled is not None and compiled.search(command):
            return pattern
    return None


def _classify_bash(policy: PermissionPolicy, command: str) -> tuple[str, str, str]:
    matched = _first_match(command, policy.full_permission_patterns)
    if matched is not None:
        action = "network_egress" if _NETWORK_COMMAND.search(command) else "global_env"
        return "full_permissions", action, f"命令匹配高权限模式 {matched!r}"
    matched = _first_match(command, policy.workspace_write_patterns)
    if matched is not None:
        return "workspace_write", "workspace_write", f"命令匹配写入模式 {matched!r}"
    return "read_only", "read", "命令未匹配任何写入或外联模式，按只读处理"


def required_level_for(
    policy: PermissionPolicy, tool_name: str, args: Mapping[str, Any]
) -> tuple[str, str, str]:
    """(所需级别, 动作类型, 原因)。顺序：显式名单 → bash 命令分类 → 未知工具兜底。"""
    if tool_name in policy.full_permission_tools:
        return "full_permissions", "privileged", "该工具被配置为需要全权限"
    if tool_name in policy.workspace_write_tools:
        return "workspace_write", "workspace_write", "该工具被配置为工作区写入类"
    if tool_name in policy.read_only_tools:
        return "read_only", "read", "该工具被配置为只读类"
    if tool_name in policy.bash_tools:
        return _classify_bash(policy, str(args.get("command") or ""))
    level = normalize_level(policy.unknown_tool_level)
    return level, "unknown", f"未知工具 {tool_name}，按配置的保守级别处理"


def _summarize(tool_name: str, args: Mapping[str, Any], bash_tools: frozenset[str]) -> str:
    if tool_name in bash_tools:
        command = str(args.get("command") or "").strip()
        return command[:_SUMMARY_LIMIT] if command else "(空命令)"
    if not args:
        return tool_name
    shown = ", ".join(f"{key}={str(value)[:60]}" for key, value in list(args.items())[:4])
    return f"{tool_name}({shown})"[:_SUMMARY_LIMIT]


def check_permission(
    policy: PermissionPolicy, current_level: object, tool_name: str, args: Mapping[str, Any]
) -> PermissionDecision:
    """一次工具调用是否越级。零 I/O，同样的输入永远得到同样的结论。"""
    current = normalize_level(current_level)
    required, action_type, reason = required_level_for(policy, tool_name, args)
    allowed = LEVEL_ORDER[required] <= LEVEL_ORDER[current]
    return PermissionDecision(
        allowed=allowed,
        required_level=required,
        current_level=current,
        action_type=action_type,
        action_summary=_summarize(tool_name, args, policy.bash_tools),
        reason=reason if allowed else f"{reason}；所需级别 {required} 高于当前级别 {current}",
    )


def action_signature(tool_name: str, args: Mapping[str, Any]) -> str:
    """会话白名单用的动作签名：工具名加完整参数的哈希，只有完全相同的调用才命中。

    与重复指纹同构但用途不同：一个表示“重复了”，一个表示“批准过”。
    """
    payload = json.dumps(
        {"tool": tool_name, "args": args}, sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
