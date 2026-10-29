"""环境变量前缀与数据目录的默认位置。

环境变量一律是 ``BASEAGENT_<名字>``；数据目录默认 ``~/.local/share/baseagent``。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

PREFIX: Final = "BASEAGENT_"

DEFAULT_HOME: Final = "~/.local/share/baseagent"


def env_value(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """取环境变量 ``BASEAGENT_<name>``；没有返回 None。

    ``name`` 不带前缀，如 ``"HOME"``、``"API_TOKEN"``。空字符串视为已设置（由调用方决定怎么处理）。
    """
    env = os.environ if environ is None else environ
    return env.get(PREFIX + name)


def pick_home(environ: Mapping[str, str] | None = None) -> Path:
    """没有显式配置时的数据目录：``BASEAGENT_HOME``，否则默认目录。"""
    explicit = env_value("HOME", environ)
    return Path(explicit or DEFAULT_HOME).expanduser()
