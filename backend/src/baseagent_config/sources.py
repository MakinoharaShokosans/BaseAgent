"""把已解析的 config.toml 内容作为一个设置来源接入 pydantic-settings。

文件内容通过上下文变量传入，而不是类属性或子类：同一个 ``Settings`` 类可以在不同线程、
不同测试里并发加载不同文件而互不串扰。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import PydanticBaseSettingsSource

#: 当前这次加载所用的配置文件内容；由 loader 在构造 Settings 的前后设置与复位
FILE_DATA: ContextVar[dict[str, Any] | None] = ContextVar(
    "baseagent_config_file_data", default=None
)


class FileDataSource(PydanticBaseSettingsSource):
    """优先级最低的来源（高于代码默认值）：config.toml 的内容。"""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # 整体由 __call__ 返回，不按字段取

    def __call__(self) -> dict[str, Any]:
        return dict(FILE_DATA.get() or {})
