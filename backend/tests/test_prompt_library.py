"""提示词库：文件是初始版本，用户覆盖优先且可恢复。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.context import PromptLibrary


@pytest.mark.unit
def test_overrides_do_not_change_the_initial_version(tmp_path: Path) -> None:
    (tmp_path / "system.md").write_text("初始系统提示词", encoding="utf-8")
    (tmp_path / "compactor.md").write_text("初始压缩提示词", encoding="utf-8")
    library = PromptLibrary(tmp_path)

    assert library.names() == ("compactor", "system")
    assert library.get("system") == "初始系统提示词"
    assert library.customized("system") is False

    library.set_override("system", "自定义系统提示词")
    assert library.get("system") == "自定义系统提示词"
    assert library.default("system") == "初始系统提示词"
    assert library.customized("system") is True

    library.set_override("system", None)
    assert library.get("system") == "初始系统提示词"
    assert library.customized("system") is False


@pytest.mark.unit
def test_blank_override_restores_the_file(tmp_path: Path) -> None:
    (tmp_path / "system.md").write_text("初始", encoding="utf-8")
    library = PromptLibrary(tmp_path, {"system": "覆盖"})
    assert library.get("system") == "覆盖"
    library.set_override("system", "   ")
    assert library.get("system") == "初始"
