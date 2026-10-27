"""日志配置：Loguru 双 sink、上下文自动注入、统一脱敏。设计文档：docs/08_observability/02 §2。

- 控制台（彩色文本）与文件（JSON Lines，``enqueue`` 后台写、轮转、保留份数）。
- ``diagnose=False``：不打印局部变量，避免密钥等敏感值进日志。
- ``patcher`` 把当前任务的 ``task_id`` 注入每条日志，并对消息和额外字段过一遍脱敏。
- ``setup_logging`` 幂等：重复调用只重建一次 sink。
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from agent_runtime.core import current_task_id
from agent_runtime.observability.redaction import Redactor

_installed: list[int] = []


def make_patcher(redactor: Redactor):  # type: ignore[no-untyped-def]
    def patch(record: Any) -> None:
        task_id = current_task_id.get()
        if task_id is not None:
            record["extra"].setdefault("task_id", task_id)
        record["message"] = redactor.text(str(record["message"]))
        cleaned = redactor.value(
            dict(record["extra"])
        )  # 键名含 password/token 等的额外字段整体替换
        if isinstance(cleaned, dict):
            record["extra"].update(cleaned)

    return patch


def setup_logging(
    *,
    level: str,
    console: bool,
    file: Path | None,
    rotation_mb: int,
    retention_files: int,
    redactor: Redactor,
) -> None:
    for handler_id in _installed:
        with contextlib.suppress(ValueError):
            logger.remove(handler_id)
    _installed.clear()
    logger.remove()  # 去掉 loguru 的默认 sink，否则会出现重复输出
    logger.configure(patcher=make_patcher(redactor))
    if console:
        _installed.append(logger.add(sys.stderr, level="INFO", backtrace=True, diagnose=False))
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.parent.chmod(0o700)
        # 先以 0600 建好文件，轮转出的新文件沿用 umask；目录是 0700，所以外部读不到
        os.close(os.open(file, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600))
        _installed.append(
            logger.add(
                file,
                level=level,
                serialize=True,
                enqueue=True,
                backtrace=True,
                diagnose=False,
                rotation=f"{rotation_mb} MB",
                retention=retention_files,
            )
        )
