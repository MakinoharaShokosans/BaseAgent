"""配置模型的校验规则。

规则出处：docs/01_architecture/04_configuration.md §6。做法：以随仓库的参考配置为基线，
每个用例只改一处，断言被拒绝且错误指向正确的位置。
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from baseagent_config import Settings


@pytest.fixture(scope="module")
def reference(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def _set(data: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    """返回改了一处的副本；路径里的数字当作列表下标，``-`` 前缀表示删除该键。"""
    out = copy.deepcopy(data)
    *parents, leaf = dotted.split(".")
    node: Any = out
    for part in parents:
        node = node[int(part)] if isinstance(node, list) else node[part]
    if value is _DELETE:
        del node[leaf]
    else:
        node[leaf] = value
    return out


_DELETE = object()


def _problems(data: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(ValidationError) as info:
        Settings.model_validate(data)
    return [(".".join(str(p) for p in e["loc"]), e["msg"]) for e in info.value.errors()]


def _rejects(data: dict[str, Any], *, at: str, containing: str = "") -> None:
    problems = _problems(data)
    assert any(loc.startswith(at) and containing in msg for loc, msg in problems), (
        f"期望在 {at!r} 处看到 {containing!r}，实际：{problems}"
    )


# ==============================================================================
# 基线
# ==============================================================================


@pytest.mark.unit
def test_reference_config_is_valid(reference: dict[str, Any]) -> None:
    settings = Settings.model_validate(reference)
    assert settings.server.port == 11328
    assert settings.models.reasoning.endpoints[0].model == "deepseek-v4-flash"


@pytest.mark.unit
def test_settings_are_immutable(reference: dict[str, Any]) -> None:
    settings = Settings.model_validate(reference)
    with pytest.raises(ValidationError):
        settings.server.port = 9  # type: ignore[misc]


# ==============================================================================
# 未知键
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "dotted",
    [
        "server.prot",  # 拼写错误
        "runtime.guardrails.max_step",
        "models.reasoning.endpoints.0.context_windw",
        "mcp.servers.github.comand",
        "tracing.otlp.endpont",
    ],
)
def test_unknown_key_is_rejected(reference: dict[str, Any], dotted: str) -> None:
    _rejects(_set(reference, dotted, 1), at=dotted, containing="Extra inputs")


@pytest.mark.unit
def test_unknown_section_is_rejected(reference: dict[str, Any]) -> None:
    data = {**copy.deepcopy(reference), "code_search": {"enabled": True}}
    _rejects(data, at="code_search", containing="Extra inputs")


# ==============================================================================
# 安全红线：回环
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize("section", ["server", "bash_shell", "web_search"])
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::", ""])
def test_non_loopback_host_is_rejected(reference: dict[str, Any], section: str, host: str) -> None:
    _rejects(_set(reference, f"{section}.host", host), at=f"{section}.host", containing="回环")


@pytest.mark.unit
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_accepted(reference: dict[str, Any], host: str) -> None:
    assert Settings.model_validate(_set(reference, "server.host", host)).server.host == host


@pytest.mark.unit
def test_block_private_addresses_must_be_a_real_bool(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "web_search.block_private_addresses", "maybe"), at="web_search")
    off = Settings.model_validate(_set(reference, "web_search.block_private_addresses", "false"))
    assert off.web_search.block_private_addresses is False


# ==============================================================================
# 路径
# ==============================================================================


@pytest.mark.unit
def test_browse_roots_may_not_sit_inside_a_forbidden_root(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "server.filesystem.browse_roots", ["/etc/ssh"]), at="server")
    _rejects(_set(reference, "server.filesystem.browse_roots", ["/etc"]), at="server")


@pytest.mark.unit
def test_whole_disk_browse_root_is_allowed_when_explicit(reference: dict[str, Any]) -> None:
    settings = Settings.model_validate(_set(reference, "server.filesystem.browse_roots", ["/"]))
    assert settings.server.filesystem.browse_roots == ["/"]


# ==============================================================================
# 模型
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize("tier", ["reasoning", "fast"])
def test_each_tier_needs_at_least_one_endpoint(reference: dict[str, Any], tier: str) -> None:
    _rejects(_set(reference, f"models.{tier}.endpoints", []), at=f"models.{tier}.endpoints")


@pytest.mark.unit
@pytest.mark.parametrize("field", ["context_window", "max_output_tokens", "timeout_sec"])
@pytest.mark.parametrize("bad", [0, -1])
def test_endpoint_numbers_must_be_positive(reference: dict[str, Any], field: str, bad: int) -> None:
    dotted = f"models.reasoning.endpoints.0.{field}"
    _rejects(_set(reference, dotted, bad), at=dotted)


@pytest.mark.unit
def test_max_output_must_fit_inside_the_window(reference: dict[str, Any]) -> None:
    data = _set(reference, "models.reasoning.endpoints.0.max_output_tokens", 1_000_000)
    _rejects(data, at="models.reasoning.endpoints.0", containing="窗口")


@pytest.mark.unit
def test_endpoint_names_are_unique_within_a_tier(reference: dict[str, Any]) -> None:
    data = copy.deepcopy(reference)
    data["models"]["reasoning"]["endpoints"].append(
        copy.deepcopy(data["models"]["reasoning"]["endpoints"][0])
    )
    _rejects(data, at="models.reasoning", containing="重名")


@pytest.mark.unit
def test_bad_capability_value_is_rejected(reference: dict[str, Any]) -> None:
    dotted = "models.reasoning.endpoints.0.capabilities.reasoning_passback"
    _rejects(_set(reference, dotted, "sometimes"), at=dotted)


@pytest.mark.unit
def test_subagent_and_research_tier_must_exist(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "research.model_tier", "turbo"), at="research.model_tier")
    _rejects(_set(reference, "subagent.model_tier", "turbo"), at="subagent.model_tier")


# ==============================================================================
# 比例与数量关系
# ==============================================================================


@pytest.mark.unit
def test_session_ratios(reference: dict[str, Any]) -> None:
    ok = _set(reference, "context.session.high_watermark", 0.9)
    assert Settings.model_validate(ok).context.session.high_watermark == 0.9
    _rejects(_set(reference, "context.session.high_watermark", 0.3), at="context.session")
    _rejects(_set(reference, "context.session.high_watermark", 1.0), at="context.session")
    _rejects(_set(reference, "context.session.compaction_ratio", 0.0), at="context.session")


@pytest.mark.unit
def test_task_ratios_and_token_lines(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "context.task.soft_ratio", 0.9), at="context.task")
    _rejects(_set(reference, "context.task.hard_ratio", 1.2), at="context.task")
    _rejects(_set(reference, "context.task.soft_tokens", 200_000), at="context.task")


@pytest.mark.unit
def test_trigger_is_the_smaller_of_ratio_and_absolute(reference: dict[str, Any]) -> None:
    task = Settings.model_validate(reference).context.task
    # 1M 窗口：比例线 60 万，绝对线 10 万，取小
    assert task.soft_trigger(1_000_000) == 100_000
    assert task.hard_trigger(1_000_000) == 160_000
    # 小窗口：比例线更小
    assert task.soft_trigger(50_000) == 30_000
    assert task.hard_trigger(50_000) == 40_000


@pytest.mark.unit
def test_mcp_call_timeout_must_be_below_dispatch_timeout(reference: dict[str, Any]) -> None:
    # 跨节规则挂在根上，错误位置为空
    _rejects(_set(reference, "mcp.call_timeout_sec", 60), at="", containing="派发")
    _rejects(_set(reference, "mcp.call_timeout_sec", 90), at="", containing="派发")


@pytest.mark.unit
def test_bash_timeout_may_not_exceed_its_maximum(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "bash_shell.timeout_sec", 601), at="bash_shell")
    ok = _set(reference, "bash_shell.timeout_sec", 600)
    assert Settings.model_validate(ok).bash_shell.timeout_sec == 600


# ==============================================================================
# 权限
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize(
    "dotted",
    [
        "permissions.default_level",
        "permissions.unknown_tool_level",
        "runtime.approval.default_level",
    ],
)
def test_permission_levels_must_be_known(reference: dict[str, Any], dotted: str) -> None:
    _rejects(_set(reference, dotted, "root"), at=dotted)


@pytest.mark.unit
@pytest.mark.parametrize("key", ["full_permission_patterns", "workspace_write_patterns"])
def test_uncompilable_permission_pattern_fails_at_load(reference: dict[str, Any], key: str) -> None:
    _rejects(
        _set(reference, f"permissions.{key}", ["(unclosed"]), at="permissions", containing="正则"
    )


@pytest.mark.unit
def test_uncompilable_redaction_pattern_fails_at_load(reference: dict[str, Any]) -> None:
    _rejects(
        _set(reference, "redaction.value_patterns", ["[a-"]), at="redaction", containing="正则"
    )


# ==============================================================================
# 子智能体
# ==============================================================================


@pytest.mark.unit
def test_subagent_must_deny_recursive_spawn(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "subagent.denied_tools", ["delegate_research"]), at="subagent")


@pytest.mark.unit
def test_subagent_depth_cannot_be_negative(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "subagent.max_depth", -1), at="subagent.max_depth")
    zero = _set(reference, "subagent.max_depth", 0)
    assert Settings.model_validate(zero).subagent.max_depth == 0


# ==============================================================================
# MCP
# ==============================================================================


@pytest.mark.unit
def test_mcp_secret_values_must_be_env_references(reference: dict[str, Any]) -> None:
    data = _set(reference, "mcp.servers.github.env", {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_plain"})
    _rejects(data, at="mcp.servers.github", containing="env:")


@pytest.mark.unit
def test_mcp_nonsecret_env_values_may_be_plain(reference: dict[str, Any]) -> None:
    data = _set(reference, "mcp.servers.github.env", {"LOG_LEVEL": "debug"})
    assert Settings.model_validate(data).mcp.servers["github"].env == {"LOG_LEVEL": "debug"}


@pytest.mark.unit
def test_mcp_stdio_needs_command_and_sse_needs_url(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "mcp.servers.github.command", ""), at="mcp.servers.github")
    _rejects(_set(reference, "mcp.servers.remote_doc.url", ""), at="mcp.servers.remote_doc")
    _rejects(_set(reference, "mcp.servers.github.transport", "carrier-pigeon"), at="mcp.servers")


# ==============================================================================
# 其他取值范围
# ==============================================================================


@pytest.mark.unit
@pytest.mark.parametrize("port", [0, -1, 65536])
def test_ports_are_in_range(reference: dict[str, Any], port: int) -> None:
    _rejects(_set(reference, "server.port", port), at="server.port")


@pytest.mark.unit
def test_log_level_is_validated(reference: dict[str, Any]) -> None:
    _rejects(_set(reference, "logging.level", "LOUD"), at="logging.level")


@pytest.mark.unit
def test_multiple_problems_are_all_reported(reference: dict[str, Any]) -> None:
    data = _set(_set(reference, "server.port", 0), "server.host", "0.0.0.0")
    locs = {loc for loc, _ in _problems(data)}
    assert {"server.port", "server.host"} <= locs
