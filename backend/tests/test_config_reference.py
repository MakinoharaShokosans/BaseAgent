"""参考配置 config/config.toml 与代码内置默认值必须一致。

“文件即规范”：文件里列出的每一项在代码里都有对应字段，代码里每一项的默认值也都写在文件里，
两边不能漂移。
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from baseagent_config import Settings


@pytest.fixture(scope="module")
def reference(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def _dump(settings: Settings) -> dict[str, Any]:
    return settings.model_dump()


@pytest.mark.unit
def test_code_defaults_equal_the_reference_file(reference: dict[str, Any]) -> None:
    """只留下必须写的 models，其余全靠代码默认值，结果应与完整文件一致。

    ``mcp.servers`` 例外：默认不预置任何服务器，文件里的三项是示例。
    """
    only_models = {"models": copy.deepcopy(reference["models"])}
    from_defaults = _dump(Settings.model_validate(only_models))
    from_file = _dump(Settings.model_validate(reference))
    from_defaults["mcp"].pop("servers")
    from_file["mcp"].pop("servers")
    assert from_defaults == from_file


def _fields(model: type[BaseModel]) -> dict[str, Any]:
    return {name: info.annotation for name, info in model.model_fields.items()}


def _walk(model: type[BaseModel], table: dict[str, Any], prefix: str) -> list[str]:
    """返回代码里有、文件里没写的键。"""
    missing: list[str] = []
    for name, annotation in _fields(model).items():
        if name not in table:
            missing.append(f"{prefix}{name}")
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            missing += _walk(annotation, table[name], f"{prefix}{name}.")
    return missing


@pytest.mark.unit
def test_every_code_field_is_documented_in_the_reference_file(reference: dict[str, Any]) -> None:
    missing = _walk(Settings, reference, "")
    assert not missing, missing


@pytest.mark.unit
def test_every_reference_section_is_known_to_the_code(reference: dict[str, Any]) -> None:
    assert set(reference) <= set(Settings.model_fields)
