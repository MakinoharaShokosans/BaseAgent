"""验证 import-linter 契约真的能拦住违规，而不是因为还没有代码而平凡成立。

做法：把 src 与 pyproject.toml 复制到临时目录，注入违规导入，运行 lint-imports，
断言对应契约被打破；再注入合法依赖，断言契约保持。契约出处见
docs/01_architecture/03_code_layout_and_roadmap.md §1。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

#: （注入的文件, 内容, 应当被打破的契约名）
VIOLATIONS = [
    (
        "src/agent_runtime/loop/_v_runtime.py",
        "import agent_runtime.runtime\n",
        "运行时分层",
    ),
    (
        "src/agent_runtime/checkpoint/_v_loop.py",
        "import agent_runtime.loop\n",
        "运行时分层",
    ),
    (
        "src/agent_runtime/approval/_v_ckpt.py",
        "import agent_runtime.checkpoint\n",
        "运行时分层",
    ),
    (
        "src/tools/core/_v_runtime.py",
        "import agent_runtime.runtime\n",
        "工具层不依赖运行时",
    ),
    (
        "src/aegis_config/_v_config.py",
        "import agent_runtime.core\n",
        "配置包零项目内依赖",
    ),
    (
        "src/agent_runtime/core/_v_core_cfg.py",
        "import aegis_config\n",
        "core 零项目内依赖",
    ),
    (
        "src/agent_runtime/core/_v_core.py",
        "import agent_runtime.loop\n",
        "core 零项目内依赖",
    ),
    (
        "src/tools/core/_v_tools.py",
        "import agent_runtime.loop\n",
        "工具层不依赖运行时",
    ),
    (
        "src/mcps/_v_mcps.py",
        "import agent_runtime.context\n",
        "工具层不依赖运行时",
    ),
    (
        "src/services/bash_shell/_v_services.py",
        "import tools.core\n",
        "sidecar 服务独立",
    ),
    (
        "src/agent_runtime/observability/_v_obs.py",
        "import agent_runtime.api\n",
        "观测不依赖业务层",
    ),
    (
        "src/agent_runtime/guardrails/_v_guard.py",
        "import tools.core\n",
        "护栏保持纯净",
    ),
    (
        "src/agent_runtime/loop/_v_layers.py",
        "import agent_runtime.api\n",
        "运行时分层",
    ),
    (
        "src/agent_runtime/llm/_v_layers2.py",
        "import agent_runtime.subagents\n",
        "运行时分层",
    ),
]

#: 合法依赖：不应打破任何契约
ALLOWED = [
    (
        "src/agent_runtime/runtime/_ok_compose.py",
        "import agent_runtime.loop\n"
        "import agent_runtime.checkpoint\n"
        "import agent_runtime.approval\n",
    ),
    ("src/agent_runtime/api/_ok_runtime.py", "import agent_runtime.runtime\n"),
    ("src/services/bash_shell/_ok_cfg.py", "import aegis_config\n"),
    ("src/tools/core/_ok_cfg.py", "import aegis_config\n"),
    ("src/agent_runtime/llm/_ok_cfg.py", "import aegis_config\n"),
    ("src/tools/core/_ok_core.py", "import agent_runtime.core\n"),
    ("src/tools/core/_ok_guard.py", "import agent_runtime.guardrails\n"),
    ("src/mcps/_ok_mcps.py", "import agent_runtime.guardrails\nimport agent_runtime.core\n"),
    ("src/agent_runtime/loop/_ok_loop.py", "import agent_runtime.llm\nimport agent_runtime.core\n"),
    ("src/agent_runtime/api/_ok_api.py", "import agent_runtime.loop\n"),
    ("src/agent_runtime/subagents/_ok_sub.py", "import agent_runtime.llm\nimport tools.core\n"),
]


def _lint(workdir: Path) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(workdir / "src")}
    code = "from importlinter.cli import lint_imports_command; lint_imports_command()"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _workspace(tmp_path: Path) -> Path:
    shutil.copytree(
        BACKEND / "src",
        tmp_path / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(BACKEND / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def _inject(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


@pytest.mark.integration
def test_clean_tree_keeps_all_contracts(tmp_path: Path) -> None:
    code, output = _lint(_workspace(tmp_path))
    assert code == 0, output
    assert "0 broken" in output


@pytest.mark.integration
@pytest.mark.parametrize(("rel", "content", "contract"), VIOLATIONS)
def test_violation_breaks_expected_contract(
    tmp_path: Path, rel: str, content: str, contract: str
) -> None:
    root = _workspace(tmp_path)
    _inject(root, rel, content)
    code, output = _lint(root)
    assert code != 0, f"{rel} 的违规没有被拦住：\n{output}"
    broken_section = output.split("Contracts:")[0]
    assert f"{contract} BROKEN" in broken_section, output


@pytest.mark.integration
def test_allowed_dependencies_do_not_break_contracts(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    for rel, content in ALLOWED:
        _inject(root, rel, content)
    code, output = _lint(root)
    assert code == 0, output
