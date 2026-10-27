"""改名前后的兼容：环境变量 ``AEGIS_*`` → ``BASEAGENT_*``，
数据目录 ``~/.local/share/aegis`` → ``~/.local/share/baseagent``。

原则：**旧的永远还能用，新的永远优先，从不替用户搬数据**。同一个变量新旧都设了，用新的；
只设了旧的，照样生效，并给出一条改名提示。旧数据目录只要还在、新目录还没建，就继续用它。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

PREFIX: Final = "BASEAGENT_"
LEGACY_PREFIX: Final = "AEGIS_"

DEFAULT_HOME: Final = "~/.local/share/baseagent"
LEGACY_HOME: Final = "~/.local/share/aegis"


def env_value(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """取环境变量 ``BASEAGENT_<name>``，没有就取旧名 ``AEGIS_<name>``；都没有返回 None。

    ``name`` 不带前缀，如 ``"HOME"``、``"API_TOKEN"``。空字符串视为已设置（由调用方决定怎么处理）。
    """
    env = os.environ if environ is None else environ
    if PREFIX + name in env:
        return env[PREFIX + name]
    return env.get(LEGACY_PREFIX + name)


def legacy_env_notes(environ: Mapping[str, str] | None = None) -> list[str]:
    """哪些变量还在用旧名（且没有被同名的新变量覆盖）：给启动时的改名提示用。变量值不出现在提示里。"""
    env = os.environ if environ is None else environ
    notes = []
    for name in sorted(env):
        if name.startswith(LEGACY_PREFIX) and (PREFIX + name[len(LEGACY_PREFIX) :]) not in env:
            new = PREFIX + name[len(LEGACY_PREFIX) :]
            notes.append(f"环境变量 {name} 已改名为 {new}；旧名仍然有效，建议改用新名")
    return notes


def pick_home(environ: Mapping[str, str] | None = None) -> Path:
    """没有显式配置时的数据目录：新目录存在或旧目录不存在 → 新目录；否则沿用旧目录。"""
    new, old = Path(DEFAULT_HOME).expanduser(), Path(LEGACY_HOME).expanduser()
    explicit = env_value("HOME", environ)
    if explicit:
        return Path(explicit).expanduser()
    return old if old.is_dir() and not new.exists() else new


def legacy_home_note(chosen: Path) -> str | None:
    old = Path(LEGACY_HOME).expanduser()
    if chosen == old:
        return (
            f"沿用旧数据目录 {old}（新默认目录是 {DEFAULT_HOME}）。"
            f"想迁移：停掉服务后执行 mv {LEGACY_HOME} {DEFAULT_HOME}"
        )
    return None
