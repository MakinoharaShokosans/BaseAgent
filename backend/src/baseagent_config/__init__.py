"""配置加载与校验。

整个项目唯一读取 config.toml 与 BASEAGENT_ 环境变量的地方。
独立成顶层包，是因为 sidecar 服务（``services``）不能依赖运行时（``agent_runtime``），
却同样要读自己的那一节配置；本包不依赖项目内任何其他部分，所有层都可以导入它。

规则与优先级见 docs/01_architecture/04_configuration.md。
"""

from baseagent_config.env import env_value, pick_home
from baseagent_config.loader import ConfigError, LoadedSettings, ResolvedPaths, load_settings
from baseagent_config.models import Endpoint, Settings

__all__ = [
    "ConfigError",
    "Endpoint",
    "LoadedSettings",
    "ResolvedPaths",
    "Settings",
    "env_value",
    "load_settings",
    "pick_home",
]
