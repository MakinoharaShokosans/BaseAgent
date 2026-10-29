"""配置加载：发现文件、合并来源、做依赖运行环境的校验，失败即拒绝启动。

优先级（高覆盖低）：显式传入（命令行） > 环境变量 BASEAGENT_<节>__<键>
> config.toml > 代码默认值。
规则出处：docs/01_architecture/04_configuration.md §2、§6。
"""

from __future__ import annotations

import os
import tomllib
import types
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin
from urllib.parse import urlparse

from pydantic import BaseModel, ValidationError

from baseagent_config.env import DEFAULT_HOME, PREFIX, env_value, pick_home
from baseagent_config.models import LOOPBACK_HOSTS, Endpoint, Settings
from baseagent_config.sources import FILE_DATA

ENV_PREFIX = PREFIX
ENV_DELIMITER = "__"

_HOME_MODE = 0o700

#: 相对路径配置项（点分路径）。相对路径一律相对数据目录解析，且不得逃出数据目录
_PATH_FIELDS = (
    "logging.file",
    "storage.metadata_db",
    "storage.checkpoint_db",
    "storage.events_db",
    "storage.traces_dir",
    "storage.artifacts_dir",
    "server.auth.token_file",
    "bash_shell.artifacts_dir",
    "web_search.artifacts_dir",
    "security.canary_salt_file",
)


class ConfigError(Exception):
    """配置不合法。``problems`` 列出全部问题，而不是遇到第一个就停。"""

    def __init__(self, problems: list[str], source: Path | None = None) -> None:
        self.problems = problems
        self.source = source
        head = f"配置不合法（{source}）" if source else "配置不合法"
        super().__init__(head + "：\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True)
class ResolvedPaths:
    """已解析为绝对路径的各项存储位置。"""

    logging_file: Path
    metadata_db: Path
    checkpoint_db: Path
    events_db: Path
    traces_dir: Path
    artifacts_dir: Path
    api_token: Path
    bash_artifacts_dir: Path
    web_artifacts_dir: Path
    canary_salt: Path


@dataclass(frozen=True)
class LoadedSettings:
    settings: Settings
    home: Path
    config_path: Path
    #: 不阻止启动但需要让人看到的事（启动看板展示）
    warnings: tuple[str, ...]
    #: 各档实际可用的端点（缺密钥的云端端点已剔除），顺序即降级顺序
    endpoints: Mapping[str, tuple[Endpoint, ...]]
    paths: ResolvedPaths

    def resolve(self, relative: str) -> Path:
        """把配置里的路径解析为绝对路径；相对路径相对数据目录，且不得逃出数据目录。"""
        problem = _resolve(self.home, relative)
        if isinstance(problem, str):
            raise ConfigError([problem])
        return problem


# ==============================================================================
# 入口
# ==============================================================================


def load_settings(
    config_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
    *,
    create_home: bool = True,
    require_models: bool = True,
) -> LoadedSettings:
    """加载并校验配置。任何问题都抛 :class:`ConfigError`，调用方应据此拒绝启动。

    ``overrides`` 对应命令行参数，嵌套字典，优先级最高。``create_home`` 为假时不创建数据目录
    （只读检查用）。``require_models`` 为假时不要求模型端点有密钥——sidecar 按设计拿不到密钥，
    只读自己那一节配置。
    """
    path = _find_config(config_path)
    file_data = _read_toml(path)

    env_problems = _check_env_overrides(os.environ)
    if env_problems:
        raise ConfigError(env_problems, path)

    settings = _build(file_data, overrides or {}, path)

    problems: list[str] = []
    home = _resolve_home(settings, problems)
    paths = _resolve_paths(settings, home, problems) if home else None
    if home and create_home:
        _ensure_home(home, problems)
    endpoints, warnings = _usable_endpoints(settings, problems if require_models else [])
    warnings += _trust_warnings(settings)

    if problems or home is None or paths is None:
        raise ConfigError(problems, path)
    return LoadedSettings(
        settings=settings,
        home=home,
        config_path=path,
        warnings=tuple(warnings),
        endpoints=endpoints,
        paths=paths,
    )


# ==============================================================================
# 文件发现与解析
# ==============================================================================


REPO_CONFIG = Path(__file__).resolve().parents[3] / "config" / "config.toml"


def _find_config(explicit: Path | None) -> Path:
    if explicit is not None:
        return _must_exist(explicit, "指定的配置文件")
    from_env = env_value("CONFIG")
    if from_env:
        return _must_exist(Path(from_env).expanduser(), "BASEAGENT_CONFIG 指向的配置文件")

    home = pick_home()
    # 源码检出里，配置在仓库根的 config/：从 backend/ 目录启动时也要找得到
    candidates = [Path.cwd() / "config" / "config.toml", home / "config.toml", REPO_CONFIG]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = "、".join(str(c) for c in candidates)
    raise ConfigError([f"未找到配置文件。已查找：{searched}；也可用 BASEAGENT_CONFIG 指定"])


def _must_exist(path: Path, what: str) -> Path:
    if not path.is_file():
        raise ConfigError([f"{what}不存在：{path}"])
    return path


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"无法解析 TOML：{exc}"], path) from exc
    except OSError as exc:
        raise ConfigError([f"无法读取配置文件：{exc}"], path) from exc


def _build(file_data: dict[str, Any], overrides: Mapping[str, Any], path: Path) -> Settings:
    token = FILE_DATA.set(file_data)
    try:
        return Settings(**dict(overrides))
    except ValidationError as exc:
        raise ConfigError(_format_validation(exc), path) from exc
    finally:
        FILE_DATA.reset(token)


def _format_validation(exc: ValidationError) -> list[str]:
    problems = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "配置"
        message = error["msg"].removeprefix("Value error, ")
        problems.append(f"{where}：{message}")
    return problems


# ==============================================================================
# 环境变量覆盖：只允许标量
# ==============================================================================


def _check_env_overrides(environ: Mapping[str, str]) -> list[str]:
    """检查 BASEAGENT_ 前缀的变量是否指向存在的标量配置项。

    pydantic-settings 会把指向数组或表的变量按 JSON 解析，文档约定这类只能改文件，
    所以在这里提前拒绝；拼写错误同样在这里报，错误信息里带上变量名。
    """
    problems = []
    for name in sorted(environ):
        if not name.startswith(ENV_PREFIX):
            continue
        parts = name[len(ENV_PREFIX) :].lower().split(ENV_DELIMITER)
        if len(parts) == 1 and parts[0] not in Settings.model_fields:
            continue  # BASEAGENT_HOME、BASEAGENT_CONFIG、BASEAGENT_API_TOKEN 等不是配置项覆盖
        problem = _check_env_path(parts)
        if problem:
            problems.append(f"环境变量 {name}：{problem}")
    return problems


def _check_env_path(parts: list[str]) -> str | None:
    model: type[BaseModel] = Settings
    walked: list[str] = []
    for index, part in enumerate(parts):
        walked.append(part)
        dotted = ".".join(walked)
        field = model.model_fields.get(part)
        if field is None:
            return f"未知配置项 {dotted}"
        kind, nested = _classify(field.annotation)
        last = index == len(parts) - 1
        if kind == "container":
            return f"{dotted} 是数组或表，不支持用环境变量覆盖，请改配置文件"
        if kind == "model":
            if last:
                return f"{dotted} 是一整节，不支持用环境变量整体覆盖，请覆盖其中的具体键"
            assert nested is not None
            model = nested
        elif not last:
            return f"{dotted} 是标量，下面没有 {parts[index + 1]}"
    return None


def _classify(annotation: Any) -> tuple[str, type[BaseModel] | None]:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        inner = [a for a in get_args(annotation) if a is not type(None)]
        return _classify(inner[0]) if len(inner) == 1 else ("scalar", None)
    if origin in (list, dict, set, tuple, frozenset):
        return "container", None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "model", annotation
    return "scalar", None


# ==============================================================================
# 数据目录与路径
# ==============================================================================


def _resolve_home(settings: Settings, problems: list[str]) -> Path | None:
    env_home = env_value("HOME")
    if env_home:
        # 环境变量由使用者在当前目录显式给出，相对路径按当前目录取绝对
        return Path(os.path.abspath(Path(env_home).expanduser()))
    configured = Path(settings.app.home).expanduser()
    if settings.app.home == DEFAULT_HOME:
        return pick_home({})
    if not configured.is_absolute():
        problems.append(
            f"app.home：{settings.app.home!r} 必须是绝对路径（或以 ~ 开头）；"
            "相对路径会让不同目录启动得到不同数据集，开发时请用环境变量 BASEAGENT_HOME"
        )
        return None
    return Path(os.path.normpath(configured))


def _ensure_home(home: Path, problems: list[str]) -> None:
    try:
        if not home.exists():
            home.mkdir(parents=True)
            home.chmod(_HOME_MODE)
    except OSError as exc:
        problems.append(f"app.home：无法创建数据目录 {home}：{exc}")
        return
    if not home.is_dir():
        problems.append(f"app.home：{home} 不是目录")
    elif not os.access(home, os.W_OK | os.X_OK):
        problems.append(f"app.home：数据目录 {home} 不可写")


def _resolve(home: Path, value: str) -> Path | str:
    """相对路径相对 home 解析；字面上逃出 home 的返回错误文本。"""
    raw = Path(value).expanduser()
    if raw.is_absolute():
        return Path(os.path.normpath(raw))
    joined = Path(os.path.normpath(home / raw))
    if joined != home and home not in joined.parents:
        return f"相对路径 {value!r} 解析后落在 app.home（{home}）之外"
    return joined


def _resolve_paths(settings: Settings, home: Path, problems: list[str]) -> ResolvedPaths | None:
    resolved: dict[str, Path] = {}
    for dotted in _PATH_FIELDS:
        node: Any = settings
        for part in dotted.split("."):
            node = getattr(node, part)
        result = _resolve(home, node)
        if isinstance(result, str):
            problems.append(f"{dotted}：{result}")
        else:
            resolved[dotted] = result
    if len(resolved) != len(_PATH_FIELDS):
        return None
    return ResolvedPaths(
        logging_file=resolved["logging.file"],
        metadata_db=resolved["storage.metadata_db"],
        checkpoint_db=resolved["storage.checkpoint_db"],
        events_db=resolved["storage.events_db"],
        traces_dir=resolved["storage.traces_dir"],
        artifacts_dir=resolved["storage.artifacts_dir"],
        api_token=resolved["server.auth.token_file"],
        bash_artifacts_dir=resolved["bash_shell.artifacts_dir"],
        web_artifacts_dir=resolved["web_search.artifacts_dir"],
        canary_salt=resolved["security.canary_salt_file"],
    )


# ==============================================================================
# 端点与告警
# ==============================================================================


def _is_local(endpoint: Endpoint) -> bool:
    return (urlparse(endpoint.base_url).hostname or "") in LOOPBACK_HOSTS


def _usable_endpoints(
    settings: Settings, problems: list[str]
) -> tuple[dict[str, tuple[Endpoint, ...]], list[str]]:
    """剔除缺密钥的云端端点；本机端点不需要密钥。某档一个不剩则记为问题。"""
    usable: dict[str, tuple[Endpoint, ...]] = {}
    warnings: list[str] = []
    for tier in ("reasoning", "fast"):
        kept: list[Endpoint] = []
        missing: list[str] = []
        for endpoint in getattr(settings.models, tier).endpoints:
            if _is_local(endpoint) or os.environ.get(endpoint.api_key_env, "").strip():
                kept.append(endpoint)
            else:
                missing.append(endpoint.api_key_env)
                warnings.append(
                    f"模型端点 {tier}/{endpoint.name} 缺少环境变量 {endpoint.api_key_env}，已跳过"
                )
        if not kept:
            needed = "、".join(dict.fromkeys(missing))
            problems.append(f"模型档 {tier} 没有可用端点：缺少环境变量 {needed}")
        usable[tier] = tuple(kept)
    return usable, warnings


def _trust_warnings(settings: Settings) -> list[str]:
    warnings = []
    if settings.skills.allow_workspace:
        warnings.append(
            "技能：已开启 skills.allow_workspace，工作区内的技能文件会被加载；"
            "克隆来路不明的仓库即可注入指令，仅在信任该工作区时使用"
        )
    if settings.context.rules.allow_workspace:
        warnings.append(
            "规则文件：已开启 context.rules.allow_workspace，工作区内的 CLAUDE.md / AGENTS.md "
            "会进入提示词；仅在信任该工作区时使用"
        )
    return warnings
