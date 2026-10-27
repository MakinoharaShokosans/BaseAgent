"""前置审计：命令黑名单。纯函数，零模型、无 I/O。设计文档：05_security/02_shell_sandbox.md §3.1。

按命令段（``;``、``&&``、``||``、``|``、换行）逐段分词后判断，所以引号、反斜杠、分隔符前后的
空白这类规避写法不改变结果。这是防误操作的软闸门，不是对抗恶意代码的隔离。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

_SEGMENT_SPLIT: Final = re.compile(r"\|\||&&|[;|\n&]")
_FORK_BOMB: Final = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")
_POWER: Final = frozenset({"shutdown", "reboot", "halt", "poweroff"})
_ROOTISH: Final = frozenset(
    {"/", "/*", "/.", "~", "~/", "~/*", "$home", "${home}", "$HOME", "${HOME}"}
)
_SENSITIVE: Final = re.compile(
    r"^(?:/etc/(?:passwd|shadow|sudoers(?:\.d(?:/.*)?)?|group|gshadow)|/boot(?:/.*)?|/sys(?:/.*)?"
    r"|/proc/sys(?:/.*)?|/dev/(?:sd[a-z]\d*|nvme\d+n\d+(?:p\d+)?|hd[a-z]\d*|vd[a-z]\d*|mem|kmem|port))$"
)
_WRITE_VERBS: Final = frozenset(
    {"cp", "mv", "tee", "truncate", "rm", "ln", "install", "dd", "chmod", "chown"}
)
_PREVIEW: Final = 200


@dataclass(frozen=True, slots=True)
class AuditHit:
    rule: str
    reason: str
    matched: str


def audit(command: str, extra_patterns: Iterable[str] = ()) -> AuditHit | None:
    """返回第一个命中的规则，没有则 None。"""
    if not command.strip():
        return AuditHit("EMPTY_COMMAND", "命令为空", "")
    flattened = " ".join(command.split())
    if _FORK_BOMB.search(flattened):
        return _hit("FORK_BOMB", "fork 炸弹", flattened)
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        found = _check_segment(tokens, segment)
        if found is not None:
            return found
    for pattern in extra_patterns:
        match = re.search(pattern, command, re.IGNORECASE)
        if match:
            return _hit("CUSTOM_DENY", f"命中自定义禁止规则 {pattern!r}", match.group(0))
    return None


def _hit(rule: str, reason: str, matched: str) -> AuditHit:
    return AuditHit(rule, reason, matched.strip()[:_PREVIEW])


def _tokens(segment: str) -> list[str]:
    try:
        raw = shlex.split(segment, posix=True)
    except ValueError:
        raw = segment.replace("'", "").replace('"', "").replace("\\", "").split()
    # 去掉前导的环境变量赋值与 sudo/env/command 一类的包装
    while raw and (
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", raw[0])
        or raw[0] in {"sudo", "env", "command", "nohup", "exec", "time"}
    ):
        raw = raw[1:]
    return raw


def _check_segment(tokens: Sequence[str], segment: str) -> AuditHit | None:
    name = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]
    flags = [a for a in args if a.startswith("-")]
    paths = [a for a in args if not a.startswith("-")]

    if (
        name == "rm"
        and _recursive(flags)
        and (any(p in _ROOTISH for p in paths) or "--no-preserve-root" in flags)
    ):
        return _hit("RM_RF_ROOT", "禁止递归删除根目录或用户主目录", segment)
    if name.startswith("mkfs"):
        return _hit("MKFS", "禁止格式化文件系统", segment)
    if name == "dd" and any(a.startswith("of=/dev/") and a != "of=/dev/null" for a in args):
        return _hit("DD_TO_DEVICE", "禁止 dd 写入裸设备", segment)
    if name in _POWER or (name == "init" and args[:1] in (["0"], ["6"])):
        return _hit("POWER_CONTROL", "禁止关机或重启", segment)
    if name == "systemctl" and any(a in {"poweroff", "reboot", "halt", "suspend"} for a in args):
        return _hit("POWER_CONTROL", "禁止关机或重启", segment)
    if (
        name in {"chmod", "chown", "chgrp"}
        and _recursive(flags)
        and any(p in _ROOTISH for p in paths)
    ):
        return _hit("RECURSIVE_ROOT_PERMISSION", "禁止对根目录递归改权限", segment)
    return _sensitive_write(name, args, segment)


def _recursive(flags: Sequence[str]) -> bool:
    return any(
        f == "--recursive" or (not f.startswith("--") and ("r" in f[1:] or "R" in f[1:]))
        for f in flags
    )


def _sensitive_write(name: str, args: Sequence[str], segment: str) -> AuditHit | None:
    targets = [a.removeprefix("of=") for a in args if not a.startswith("-")]
    redirects = _redirect_targets(segment)
    written = (
        bool(redirects)
        or name in _WRITE_VERBS
        or (name == "sed" and any(a.startswith("-i") for a in args))
    )
    if not written:
        return None
    for target in (*redirects, *targets):
        if _SENSITIVE.match(target):
            return _hit("SENSITIVE_SYSTEM_WRITE", f"禁止写入系统敏感路径 {target}", segment)
    return None


def _redirect_targets(segment: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"(?:\d?>>?|&>>?)\s*([^\s;&|<>]+)", segment)]
