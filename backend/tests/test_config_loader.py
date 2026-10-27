"""配置加载器：文件发现、优先级、环境变量覆盖、路径解析、端点跳过与告警。

规则出处：docs/01_architecture/04_configuration.md §2、§6。
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from baseagent_config import ConfigError, LoadedSettings, load_settings

#: 最小可用配置：只有两档模型（云端端点，需要密钥）
MINIMAL = """
[models.reasoning]
[[models.reasoning.endpoints]]
name = "r"
base_url = "https://api.example.com"
model = "m-big"
api_key_env = "TEST_API_KEY"
timeout_sec = 60
context_window = 100_000
max_output_tokens = 4096

[models.fast]
[[models.fast.endpoints]]
name = "f"
base_url = "https://api.example.com"
model = "m-small"
api_key_env = "TEST_API_KEY"
timeout_sec = 30
context_window = 100_000
max_output_tokens = 2048
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """清掉会影响加载的变量，并让工作目录落在空的临时目录，避免读到仓库里的配置。"""
    for name in list(os.environ):
        if name.startswith(("BASEAGENT_", "AEGIS_")) or name == "TEST_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEST_API_KEY", "sk-test")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "baseagent_home"
    monkeypatch.setenv("BASEAGENT_HOME", str(path))
    return path


def _write(tmp_path: Path, body: str = MINIMAL, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _load(tmp_path: Path, body: str = MINIMAL, **kwargs: object) -> LoadedSettings:
    return load_settings(config_path=_write(tmp_path, body), **kwargs)  # type: ignore[arg-type]


def _fails(tmp_path: Path, body: str = MINIMAL, **kwargs: object) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, body, **kwargs)
    return info.value


# ==============================================================================
# 基本加载
# ==============================================================================


@pytest.mark.unit
def test_minimal_config_loads_with_code_defaults(tmp_path: Path, home: Path) -> None:
    loaded = _load(tmp_path)
    assert loaded.settings.server.port == 11328  # 来自代码默认值
    assert loaded.home == home
    assert loaded.warnings == ()


@pytest.mark.unit
def test_reference_config_loads_cleanly(
    config_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    loaded = load_settings(config_path=config_path)
    assert loaded.warnings == ()
    assert [e.name for e in loaded.endpoints["reasoning"]] == ["deepseek-pro"]
    assert [e.name for e in loaded.endpoints["fast"]] == ["deepseek-flash"]


@pytest.mark.unit
def test_invalid_toml_is_a_config_error(tmp_path: Path, home: Path) -> None:
    error = _fails(tmp_path, "[server\nport = ")
    assert "config.toml" in str(error)


@pytest.mark.unit
def test_all_problems_are_listed_together(tmp_path: Path, home: Path) -> None:
    error = _fails(tmp_path, MINIMAL + '\n[server]\nport = 0\nhost = "0.0.0.0"\n')
    assert len(error.problems) >= 2
    assert any("server.port" in p for p in error.problems)
    assert any("server.host" in p for p in error.problems)


# ==============================================================================
# 配置文件发现
# ==============================================================================


@pytest.mark.unit
def test_explicit_argument_beats_env_var(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arg = _write(tmp_path, MINIMAL + "\n[server]\nport = 1111\n", "arg.toml")
    env = _write(tmp_path, MINIMAL + "\n[server]\nport = 2222\n", "env.toml")
    monkeypatch.setenv("BASEAGENT_CONFIG", str(env))
    assert load_settings(config_path=arg).settings.server.port == 1111
    assert load_settings().settings.server.port == 2222


@pytest.mark.unit
def test_discovery_prefers_cwd_config_over_home(tmp_path: Path, home: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.toml").write_text(
        MINIMAL + "\n[server]\nport = 3333\n", encoding="utf-8"
    )
    home.mkdir()
    (home / "config.toml").write_text(MINIMAL + "\n[server]\nport = 4444\n", encoding="utf-8")
    assert load_settings().settings.server.port == 3333


@pytest.mark.unit
def test_the_repo_config_is_the_last_fallback(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = home.parent / "repo.toml"
    repo.write_text(MINIMAL + "\n[server]\nport = 5555\n", encoding="utf-8")
    monkeypatch.setattr("baseagent_config.loader.REPO_CONFIG", repo)
    assert load_settings().settings.server.port == 5555


@pytest.mark.unit
def test_discovery_falls_back_to_home(tmp_path: Path, home: Path) -> None:
    home.mkdir()
    (home / "config.toml").write_text(MINIMAL + "\n[server]\nport = 4444\n", encoding="utf-8")
    assert load_settings().settings.server.port == 4444


@pytest.mark.unit
def test_missing_explicit_config_is_an_error(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BASEAGENT_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError, match=r"nope\.toml"):
        load_settings()


@pytest.mark.unit
def test_no_config_anywhere_is_an_error(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("baseagent_config.loader.REPO_CONFIG", home / "no_repo_config.toml")
    with pytest.raises(ConfigError, match="未找到"):
        load_settings()


# ==============================================================================
# 优先级：命令行 > 环境变量 > 文件 > 默认
# ==============================================================================


@pytest.mark.unit
def test_precedence_file_env_overrides(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = MINIMAL + "\n[server]\nport = 1000\n"
    assert _load(tmp_path, body).settings.server.port == 1000
    monkeypatch.setenv("BASEAGENT_SERVER__PORT", "2000")
    assert _load(tmp_path, body).settings.server.port == 2000
    assert _load(tmp_path, body, overrides={"server": {"port": 3000}}).settings.server.port == 3000


@pytest.mark.unit
def test_env_override_reaches_deeply_nested_keys(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BASEAGENT_RUNTIME__GUARDRAILS__MAX_STEPS", "40")
    assert _load(tmp_path).settings.runtime.guardrails.max_steps == 40


@pytest.mark.unit
def test_env_override_merges_instead_of_replacing_the_section(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BASEAGENT_MODELS__FAST__TEMPERATURE", "0.7")
    loaded = _load(tmp_path)
    assert loaded.settings.models.fast.temperature == 0.7
    assert loaded.settings.models.fast.endpoints[0].name == "f"  # 端点列表保留


@pytest.mark.unit
def test_env_override_with_bad_value_is_rejected(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BASEAGENT_SERVER__PORT", "eighty")
    assert any("server.port" in p for p in _fails(tmp_path).problems)


@pytest.mark.security
@pytest.mark.unit
def test_env_cannot_turn_listen_address_non_loopback(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BASEAGENT_SERVER__HOST", "0.0.0.0")
    assert any("回环" in p for p in _fails(tmp_path).problems)


@pytest.mark.unit
def test_env_typo_is_rejected(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASEAGENT_SERVER__PROT", "9000")
    assert any("server.prot" in p for p in _fails(tmp_path).problems)


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "BASEAGENT_SERVER__CORS_ALLOW_ORIGINS",  # 列表
        "BASEAGENT_SERVER",  # 整节
        "BASEAGENT_SERVER__AUTH",  # 子表
        "BASEAGENT_MODELS__REASONING__ENDPOINTS",  # 端点列表
        "BASEAGENT_MCP__SERVERS__GITHUB__ENABLED",  # 字典里的项
    ],
)
def test_env_cannot_override_arrays_or_tables(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, '["x"]')
    error = _fails(tmp_path)
    assert any("环境变量" in p and name in p for p in error.problems), error.problems


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["BASEAGENT_HOME", "BASEAGENT_CONFIG", "BASEAGENT_API_TOKEN", "BASEAGENT_PROXY_TARGET"]
)
def test_reserved_variables_are_not_config_overrides(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    if name != "BASEAGENT_HOME":
        monkeypatch.setenv(name, str(tmp_path / "x") if name == "BASEAGENT_CONFIG" else "v")
    if name == "BASEAGENT_CONFIG":
        shutil.copy(_write(tmp_path), tmp_path / "x")
    assert _load(tmp_path).warnings == ()


# ==============================================================================
# 数据目录与路径
# ==============================================================================


@pytest.mark.unit
def test_env_home_beats_file_home(tmp_path: Path, home: Path) -> None:
    other = tmp_path / "from_file"
    loaded = _load(tmp_path, f'[app]\nhome = "{other}"\n' + MINIMAL)
    assert loaded.home == home
    assert not other.exists()


@pytest.mark.unit
def test_file_home_is_used_when_env_is_absent(tmp_path: Path) -> None:
    target = tmp_path / "from_file"
    loaded = _load(tmp_path, f'[app]\nhome = "{target}"\n' + MINIMAL)
    assert loaded.home == target


@pytest.mark.unit
def test_tilde_in_home_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    loaded = _load(tmp_path, '[app]\nhome = "~/data"\n' + MINIMAL)
    assert loaded.home == tmp_path / "data"


@pytest.mark.unit
def test_relative_home_in_file_is_rejected(tmp_path: Path) -> None:
    error = _fails(tmp_path, '[app]\nhome = "./storage"\n' + MINIMAL)
    assert any("app.home" in p and "绝对" in p for p in error.problems)


@pytest.mark.unit
def test_relative_home_in_env_is_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BASEAGENT_HOME", "storage")
    assert _load(tmp_path).home == tmp_path / "storage"


@pytest.mark.unit
def test_home_is_created_private(tmp_path: Path, home: Path) -> None:
    _load(tmp_path)
    assert home.is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


@pytest.mark.unit
def test_home_creation_can_be_skipped(tmp_path: Path, home: Path) -> None:
    _load(tmp_path, create_home=False)
    assert not home.exists()


@pytest.mark.unit
@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受目录权限限制")
def test_unwritable_home_refuses_to_start(tmp_path: Path, home: Path) -> None:
    home.mkdir()
    home.chmod(0o500)
    try:
        error = _fails(tmp_path)
    finally:
        home.chmod(0o700)
    assert any("app.home" in p and "写" in p for p in error.problems)


@pytest.mark.unit
def test_relative_paths_resolve_under_home(tmp_path: Path, home: Path) -> None:
    loaded = _load(tmp_path)
    assert loaded.resolve("logs/baseagent.jsonl") == home / "logs" / "baseagent.jsonl"
    assert loaded.paths.metadata_db == home / "baseagent_meta.db"
    assert loaded.paths.canary_salt == home / "canary_salt"
    assert loaded.paths.api_token == home / "api_token"


@pytest.mark.unit
def test_absolute_paths_pass_through(tmp_path: Path, home: Path) -> None:
    body = MINIMAL + '\n[storage]\ntraces_dir = "/var/tmp/baseagent-traces"\n'
    assert _load(tmp_path, body).paths.traces_dir == Path("/var/tmp/baseagent-traces")


@pytest.mark.security
@pytest.mark.unit
@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("storage", "traces_dir", "../outside"),
        ("storage", "metadata_db", "a/../../outside.db"),
        ("logging", "file", "../../etc/x.log"),
        ("server.auth", "token_file", "../token"),
        ("security", "canary_salt_file", "../salt"),
    ],
)
def test_relative_path_may_not_escape_home(
    tmp_path: Path, home: Path, section: str, key: str, value: str
) -> None:
    error = _fails(tmp_path, MINIMAL + f'\n[{section}]\n{key} = "{value}"\n')
    assert any(f"{section}.{key}" in p and "app.home" in p for p in error.problems), error.problems


@pytest.mark.unit
def test_dotdot_that_stays_inside_home_is_fine(tmp_path: Path, home: Path) -> None:
    body = MINIMAL + '\n[storage]\ntraces_dir = "a/../traces"\n'
    assert _load(tmp_path, body).paths.traces_dir == home / "traces"


@pytest.mark.security
@pytest.mark.unit
def test_resolve_refuses_escape(tmp_path: Path, home: Path) -> None:
    with pytest.raises(ConfigError, match=r"app\.home"):
        _load(tmp_path).resolve("../elsewhere")


# ==============================================================================
# 端点：缺密钥的云端端点被跳过
# ==============================================================================

TWO_ENDPOINTS = MINIMAL.replace(
    "[models.fast]",
    """[[models.reasoning.endpoints]]
name = "r2"
base_url = "https://other.example.com"
model = "m-big-2"
api_key_env = "OTHER_KEY"
timeout_sec = 60
context_window = 100_000
max_output_tokens = 4096

[models.fast]""",
)


@pytest.mark.unit
def test_cloud_endpoint_without_key_is_skipped_with_a_warning(tmp_path: Path, home: Path) -> None:
    loaded = _load(tmp_path, TWO_ENDPOINTS)
    assert [e.name for e in loaded.endpoints["reasoning"]] == ["r"]
    assert any("r2" in w and "OTHER_KEY" in w for w in loaded.warnings)
    # 配置本身仍保留两个端点，跳过只影响运行时可用列表
    assert len(loaded.settings.models.reasoning.endpoints) == 2


@pytest.mark.unit
def test_empty_key_counts_as_missing(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTHER_KEY", "   ")
    loaded = _load(tmp_path, TWO_ENDPOINTS)
    assert [e.name for e in loaded.endpoints["reasoning"]] == ["r"]


@pytest.mark.unit
def test_present_key_keeps_order(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTHER_KEY", "k")
    loaded = _load(tmp_path, TWO_ENDPOINTS)
    assert [e.name for e in loaded.endpoints["reasoning"]] == ["r", "r2"]
    assert loaded.warnings == ()


@pytest.mark.unit
def test_tier_with_no_usable_endpoint_refuses_to_start(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_API_KEY")
    error = _fails(tmp_path)
    assert any("reasoning" in p and "TEST_API_KEY" in p for p in error.problems)
    assert any("fast" in p for p in error.problems)


@pytest.mark.unit
def test_local_endpoint_does_not_need_a_key(tmp_path: Path, home: Path) -> None:
    body = MINIMAL.replace("https://api.example.com", "http://127.0.0.1:11434").replace(
        'api_key_env = "TEST_API_KEY"', 'api_key_env = "NO_SUCH_KEY"'
    )
    loaded = _load(tmp_path, body)
    assert len(loaded.endpoints["reasoning"]) == 1
    assert len(loaded.endpoints["fast"]) == 1
    assert loaded.warnings == ()


# ==============================================================================
# 告警
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    ("section", "needle"),
    [("skills", "技能"), ("context.rules", "规则")],
)
def test_workspace_trust_switches_warn(
    tmp_path: Path, home: Path, section: str, needle: str
) -> None:
    quiet = _load(tmp_path)
    assert quiet.warnings == ()
    loud = _load(tmp_path, MINIMAL + f"\n[{section}]\nallow_workspace = true\n")
    assert any(needle in w and "allow_workspace" in w for w in loud.warnings)
