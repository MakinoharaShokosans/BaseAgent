"""共享夹具。

原则（见 docs/11_testing/01_strategy.md）：默认离线、确定、隔离。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.core import AgentState, new_state

#: 仓库根目录（backend 的上一级）
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_environment() -> Iterator[None]:
    """CLI 入口会读取仓库根真实的 .env 并写进进程环境；每个测试结束后还原，免得污染后面的测试。"""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """仓库根目录。"""
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_path(repo_root: Path) -> Path:
    """随仓库提供的参考配置文件。"""
    return repo_root / "config" / "config.toml"


@pytest.fixture
def make_state() -> Callable[..., AgentState]:
    """构造 AgentState 的工厂；关键字参数覆盖初始化字段。"""

    def factory(task_id: str = "t1", **overrides: Any) -> AgentState:
        base: dict[str, Any] = {
            "workspace_id": "w1",
            "workspace_path": "/work/proj",
            "session_id": "s1",
            "task_id": task_id,
            "task_goal": "fix the bug",
            "permission_level": "workspace_write",
            "canary_token": "canary-abc",
        }
        return new_state(**{**base, **overrides})

    return factory
