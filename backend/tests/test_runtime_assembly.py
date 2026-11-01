"""从配置装配运行时参数：循环护栏、并发名额、权限基线。出处：docs/01_architecture/04_configuration.md。"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.runtime import (
    FullPermissionsNotConfirmedError,
    max_concurrent_tasks,
    resolve_permission_level,
)
from baseagent_config import Settings


@pytest.fixture
def raw(config_path: Path) -> dict[str, Any]:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def settings_with(raw: dict[str, Any], section: str, key: str, value: Any) -> Settings:
    data = copy.deepcopy(raw)
    data[section][key] = value
    return Settings.model_validate(data)


@pytest.mark.unit
def test_there_are_no_budget_settings(raw: dict[str, Any]) -> None:
    """决策 0006：没有 Token、步数、挂钟预算，也没有预算装配函数。"""
    import agent_runtime.runtime as runtime

    guardrails = Settings.model_validate(raw).runtime.guardrails.model_dump()
    assert not set(guardrails) & {
        "max_steps",
        "max_total_tokens",
        "max_wall_time_sec",
        "warning_ratio",
    }
    assert not hasattr(runtime, "budget_limits_from_settings")
    for name in ("max_total_tokens", "min_token_budget", "max_spawns_per_task"):
        assert name not in Settings.model_validate(raw).subagent.model_dump()


@pytest.mark.unit
def test_concurrency_slots_come_from_the_server_section(raw: dict[str, Any]) -> None:
    assert max_concurrent_tasks(Settings.model_validate(raw)) == 1
    assert max_concurrent_tasks(settings_with(raw, "server", "max_concurrent_tasks", 3)) == 3


@pytest.mark.unit
def test_permission_defaults_to_the_configured_baseline(raw: dict[str, Any]) -> None:
    settings = Settings.model_validate(raw)
    assert resolve_permission_level(settings, None, confirmed=False) == "workspace_write"


@pytest.mark.unit
def test_permission_baseline_is_configurable(raw: dict[str, Any]) -> None:
    data = copy.deepcopy(raw)
    data["runtime"]["approval"]["default_level"] = "read_only"
    settings = Settings.model_validate(data)
    assert resolve_permission_level(settings, None, confirmed=False) == "read_only"


@pytest.mark.unit
@pytest.mark.parametrize("level", ["read_only", "workspace_write"])
def test_lower_levels_need_no_confirmation(raw: dict[str, Any], level: str) -> None:
    assert resolve_permission_level(Settings.model_validate(raw), level, confirmed=False) == level


@pytest.mark.unit
def test_full_permissions_needs_explicit_confirmation(raw: dict[str, Any]) -> None:
    settings = Settings.model_validate(raw)
    with pytest.raises(FullPermissionsNotConfirmedError):
        resolve_permission_level(settings, "full_permissions", confirmed=False)
    assert (
        resolve_permission_level(settings, "full_permissions", confirmed=True) == "full_permissions"
    )


@pytest.mark.unit
def test_confirmation_requirement_can_be_switched_off(raw: dict[str, Any]) -> None:
    data = copy.deepcopy(raw)
    data["runtime"]["approval"]["require_full_permissions_confirm"] = False
    settings = Settings.model_validate(data)
    assert (
        resolve_permission_level(settings, "full_permissions", confirmed=False)
        == "full_permissions"
    )


@pytest.mark.unit
def test_unknown_levels_are_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="root"):
        resolve_permission_level(Settings.model_validate(raw), "root", confirmed=True)


@pytest.mark.unit
def test_a_default_of_full_permissions_still_needs_confirmation(raw: dict[str, Any]) -> None:
    """基线被配置成 full_permissions 时，不给请求值也不能绕过确认。"""
    data = copy.deepcopy(raw)
    data["runtime"]["approval"]["default_level"] = "full_permissions"
    settings = Settings.model_validate(data)
    with pytest.raises(FullPermissionsNotConfirmedError):
        resolve_permission_level(settings, None, confirmed=False)


# ==============================================================================
# 工具阶段的配置装配
# ==============================================================================


@pytest.mark.unit
def test_the_permission_policy_follows_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.guardrails import check_permission
    from agent_runtime.runtime import permission_policy_from_settings

    policy = permission_policy_from_settings(Settings.model_validate(raw))
    assert "view_file" in policy.read_only_tools
    assert "write_file" in policy.workspace_write_tools
    assert policy.bash_tools == frozenset({"bash"})
    assert policy.unknown_tool_level == "full_permissions"
    # 参考配置里的正则是单引号字面量，装配后仍能判定
    assert (
        check_permission(policy, "workspace_write", "bash", {"command": "git push"}).allowed
        is False
    )
    assert (
        check_permission(policy, "workspace_write", "bash", {"command": "pytest -q"}).allowed
        is True
    )
    assert check_permission(policy, "read_only", "mcp_new_tool", {}).allowed is False


@pytest.mark.unit
def test_loop_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.guardrails import LoopLimits
    from agent_runtime.runtime import loop_limits_from_settings

    assert loop_limits_from_settings(Settings.model_validate(raw)) == LoopLimits(
        consecutive_errors_limit=3,
        identical_fingerprint_limit=3,
        alternation_cycles=3,
        max_consecutive_blocks=5,
    )


@pytest.mark.unit
def test_loop_settings_follow_the_reference_config(raw: dict[str, Any]) -> None:
    loop = Settings.model_validate(raw).runtime.loop
    assert (loop.model_tier, loop.call_timeout_sec, loop.alternation_cycles) == (
        "reasoning",
        300,
        3,
    )


@pytest.mark.unit
def test_mcp_exposure_defaults_to_deferred_and_servers_can_override(raw: dict[str, Any]) -> None:
    data = copy.deepcopy(raw)
    data["mcp"]["servers"]["docs"] = {"enabled": False, "command": "x", "exposure": "pinned"}
    mcp = Settings.model_validate(data).mcp
    assert mcp.exposure == "deferred" and mcp.servers["docs"].exposure == "pinned"
    assert (mcp.index_max_listed, mcp.index_desc_chars) == (60, 80)


@pytest.mark.unit
def test_observation_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.context import ObservationLimits
    from agent_runtime.runtime import observation_limits_from_settings

    limits = observation_limits_from_settings(Settings.model_validate(raw))
    assert limits == ObservationLimits(
        max_tokens=1500,
        head_lines=20,
        tail_lines=30,
        keyword_lines_limit=30,
        json_outline_max_depth=3,
        json_outline_array_items=5,
        scalar_preview_chars=120,
        exempt_tools=("load_skill", "mcp_describe"),
        exempt_multiplier=8,
        passthrough_tools=("view_file", "grep", "list_dir", "read_artifact", "websearch"),
        passthrough_max_tokens=12_000,
    )


@pytest.mark.unit
def test_the_real_approval_hooks_are_wired() -> None:
    from agent_runtime.approval import check_binding, compute_approval_id
    from agent_runtime.runtime import APPROVAL_HOOKS

    assert APPROVAL_HOOKS.approval_id is compute_approval_id
    assert APPROVAL_HOOKS.check_binding is check_binding


@pytest.mark.unit
def test_file_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.runtime import file_limits_from_settings
    from tools.builtin import FileLimits

    assert file_limits_from_settings(Settings.model_validate(raw)) == FileLimits(
        view_max_lines=400, view_max_file_bytes=10 * 1024 * 1024, write_max_bytes=1_000_000
    )


@pytest.mark.unit
def test_skill_limits_follow_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.runtime import skill_limits_from_settings
    from agent_runtime.skills import SkillLimits

    assert skill_limits_from_settings(Settings.model_validate(raw)) == SkillLimits(
        max_description_chars=200,
        max_triggers=12,
        high_privilege_tools=frozenset({"bash", "write_file", "edit_file"}),
        protect_trusted_names=True,
        scan_untrusted_body=True,
    )


@pytest.mark.unit
def test_skill_roots_follow_the_source_switches(raw: dict[str, Any], tmp_path: Path) -> None:
    from agent_runtime.runtime import skill_roots_from_settings

    settings = Settings.model_validate(raw)
    default = skill_roots_from_settings(settings, tmp_path, global_dir=tmp_path / "g")
    assert [r.source for r in default] == ["builtin", "global"]
    assert default[0].path.name == "skills" and default[1].path == tmp_path / "g"

    data = copy.deepcopy(raw)
    data["skills"]["allow_workspace"] = True
    opened = skill_roots_from_settings(Settings.model_validate(data), tmp_path)
    assert next(iter(opened)).source == "workspace"
    assert opened[0].path == tmp_path / ".baseagent" / "skills"


@pytest.mark.unit
def test_workspace_rules_stay_off_unless_the_config_opts_in(
    raw: dict[str, Any], tmp_path: Path
) -> None:
    from agent_runtime.context import RulesCache
    from agent_runtime.runtime import rules_from_settings

    (tmp_path / "AGENTS.md").write_text("规则")
    assert (
        rules_from_settings(Settings.model_validate(raw), tmp_path, RulesCache()).status
        == "disabled"
    )
    data = copy.deepcopy(raw)
    data["context"]["rules"]["allow_workspace"] = True
    opened = rules_from_settings(Settings.model_validate(data), tmp_path, RulesCache())
    assert opened.status == "enabled" and opened.rules is not None


@pytest.mark.unit
def test_prompt_assets_and_the_assembler_follow_the_config(raw: dict[str, Any]) -> None:
    from agent_runtime.context import PromptLibrary
    from agent_runtime.runtime import assembler_from_settings, build_prompt_assets

    assets = build_prompt_assets(
        PromptLibrary(), workspace_name="p", tool_names=["view_file", "bash"]
    )
    assert assets.tool_names == ("bash", "view_file") and "数据隔离" in assets.system
    assert assets.rules_status == "absent" and assets.skills_manifest == ""

    default = assembler_from_settings(Settings.model_validate(raw), assets)
    data = copy.deepcopy(raw)
    data["context"]["prompt"]["goal_echo_chars"] = 50
    tuned = assembler_from_settings(Settings.model_validate(data), assets)
    assert default._goal_chars == 300 and tuned._goal_chars == 50  # type: ignore[reportPrivateUsage]


@pytest.mark.unit
def test_compaction_config_follows_the_reference_config(raw: dict[str, Any]) -> None:
    from agent_runtime.memory import CompactionConfig, MergeLimits
    from agent_runtime.runtime import compaction_config_from_settings

    assert compaction_config_from_settings(Settings.model_validate(raw)) == CompactionConfig(
        token_limit=32000,
        high_watermark=0.8,
        compaction_ratio=0.4,
        keep_recent_turns=3,
        backoff_sec=(30, 120, 600),
        limits=MergeLimits(
            max_facts=50, max_failures=30, summary_max_chars=300, require_provenance=True
        ),
    )


@pytest.mark.unit
async def test_the_memory_flow_is_built_from_the_memory_section(
    raw: dict[str, Any], tmp_path: Path
) -> None:
    from agent_runtime.memory import MemoryStore
    from agent_runtime.runtime import memory_flow_from_settings

    async def no_extractor(request: Any) -> None:
        return None

    store = MemoryStore(tmp_path / "m.db")
    data = copy.deepcopy(raw)
    data["context"]["memory"].update(max_pending_facts=7, persist_task_facts=False)
    data["context"]["session"]["active_window_turns"] = 5
    flow = memory_flow_from_settings(Settings.model_validate(data), store, no_extractor, len)
    assert (flow._max_pending, flow._persist, flow._window) == (7, False, 5)  # type: ignore[reportPrivateUsage]


@pytest.mark.unit
def test_the_fold_policy_follows_the_task_section_and_takes_the_smaller_trigger(
    raw: dict[str, Any],
) -> None:
    from agent_runtime.context import FoldPolicy
    from agent_runtime.runtime import fold_policy_from_settings

    s = Settings.model_validate(raw)
    assert fold_policy_from_settings(s, 128_000) == FoldPolicy(
        76_800,
        3,
        frozenset({"load_skill", "mcp_describe"}),
        frozenset({"view_file", "grep", "list_dir", "read_artifact"}),
    )
    assert fold_policy_from_settings(s, 1_000_000).trigger_tokens == 100_000  # 绝对上限生效


@pytest.mark.unit
def test_the_assembler_folds_only_when_a_window_is_given(raw: dict[str, Any]) -> None:
    from agent_runtime.context import PromptAssets
    from agent_runtime.runtime import assembler_from_settings

    assets = PromptAssets(system="s", workspace_name="w")
    s = Settings.model_validate(raw)
    assert assembler_from_settings(s, assets)._fold is None  # type: ignore[reportPrivateUsage]
    folded = assembler_from_settings(s, assets, context_window=128_000)
    assert folded._fold is not None  # type: ignore[reportPrivateUsage]


@pytest.mark.unit
async def test_workspace_capability_isolation_assembly(
    config_path: Path, tmp_path: Path
) -> None:
    """ADR 0008：工作区能力按需隔离，装配期根据白名单/黑名单过滤工具、MCP 与技能。"""
    from collections.abc import Mapping

    from agent_runtime.checkpoint import CheckpointStore
    from agent_runtime.core import AgentTool, Message, ToolCall, ToolResult, new_state
    from agent_runtime.memory import MemoryStore
    from agent_runtime.observability import EventHub, Redactor
    from agent_runtime.runtime import memory_flow_from_settings
    from agent_runtime.runtime.factory import RuntimeFactory
    from agent_runtime.runtime.overrides import WorkspaceSettings, save_workspace_settings
    from baseagent_config.loader import load_settings

    # 1. 准备工作区根目录与自定义技能目录
    ws_root = tmp_path / "work"
    ws_root.mkdir()
    skills_dir = ws_root / ".baseagent" / "skills"
    # 创建两个技能：一个 xxx，一个 normal_skill
    (skills_dir / "xxx").mkdir(parents=True)
    (skills_dir / "xxx" / "SKILL.md").write_text(
        "---\nname: xxx\ndescription: skill xxx\n---\n# xxx\n", encoding="utf-8"
    )
    (skills_dir / "normal_skill").mkdir(parents=True)
    (skills_dir / "normal_skill" / "SKILL.md").write_text(
        "---\nname: normal_skill\ndescription: normal\n---\n# normal\n", encoding="utf-8"
    )

    loaded = load_settings(
        config_path=config_path,
        overrides={
            "storage": {
                "metadata_db": str(tmp_path / "m.db"),
                "checkpoint_db": str(tmp_path / "c.db"),
                "events_db": str(tmp_path / "e.db"),
                "artifacts_dir": str(tmp_path / "artifacts"),
                "traces_dir": str(tmp_path / "traces"),
            },
            "skills": {"allow_workspace": True},
        },
        require_models=False,
    )

    async with (
        MemoryStore(tmp_path / "m.db") as memory,
        CheckpointStore(tmp_path / "c.db") as checkpoints,
    ):
        await memory.create_workspace("w1", "proj", str(ws_root))
        await memory.create_session("s1", "w1")

        # 设置工作区隔离配置：
        # - disabled_tools=["bash", "write_file"]
        # - disabled_skills=["xxx"]
        # - enabled_mcp_servers=["allowed_server"]
        ws_settings = WorkspaceSettings(
            disabled_tools=["bash", "write_file"],
            disabled_skills=["xxx"],
            enabled_mcp_servers=["allowed_server"],
        )
        await save_workspace_settings(memory, "w1", ws_settings)

        class FakeMcpTool(AgentTool):
            trust = "untrusted"

            def __init__(self, server: str, name: str) -> None:
                self.server = server
                self.name = name
                self.description = f"desc of {name}"
                self.parameters = {}
                self.idempotent = False
                self.timeout_sec = 10.0

            async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
                return ToolResult(ok=True, content="ok")

        class FakeMcpManager:
            async def tools(self) -> list[Any]:
                return [
                    FakeMcpTool(server="allowed_server", name="allowed_tool"),
                    FakeMcpTool(server="blocked_server", name="blocked_tool"),
                ]

            def exposure_of(self, server: str) -> str:
                return "pinned"

            def is_idempotent(self, name: str) -> bool:
                return False

        class DummyGateway:
            async def invoke(self, request: Any) -> Any:
                return Message(role="assistant", content="done")

        hub = EventHub(clip_chars=300)
        redactor = Redactor(["key"], [])

        async def no_extractor(request: Any) -> None:
            return None

        flow = memory_flow_from_settings(loaded.settings, memory, no_extractor, len)

        factory = RuntimeFactory(
            loaded=loaded,
            gateway=DummyGateway(),  # type: ignore[arg-type]
            checkpoints=checkpoints,
            memory=memory,
            hub=hub,
            redactor=redactor,
            flow=flow,
            mcp=FakeMcpManager(),  # type: ignore[arg-type]
        )

        state = new_state(
            workspace_id="w1",
            workspace_path=str(ws_root),
            session_id="s1",
            task_id="t1",
            task_goal="test",
            permission_level="workspace_write",
            canary_token="canary-123",
        )

        # 1. 验证装配资产中的技能清单：排除了 xxx，保留了 normal_skill
        assembled = await factory._assemble(state)
        assert "xxx" not in assembled.assets.skills_manifest
        assert "normal_skill" in assembled.assets.skills_manifest

        # 2. 调用 factory.build_runtime(state) 构建 TaskRuntime
        runtime = await factory.build_runtime(state)

        # 提取 runtime 中的 ToolRegistry
        registry = None
        for cell in getattr(runtime.tools, "__closure__", ()) or ():
            obj = cell.cell_contents
            if hasattr(obj, "dispatcher") and hasattr(obj.dispatcher, "_registry"):
                registry = obj.dispatcher._registry
                break
        assert registry is not None

        # 验证 disabled_tools: 不包含 bash 与 write_file，但包含 view_file
        assert not registry.knows("bash")
        assert not registry.knows("write_file")
        assert "bash" not in {s.name for s in registry.schemas()}
        assert "write_file" not in {s.name for s in registry.schemas()}
        assert registry.knows("view_file")

        # 验证 enabled_mcp_servers: 包含 allowed_tool，拦截过滤 blocked_tool
        assert registry.knows("allowed_tool")
        assert not registry.knows("blocked_tool")
        assert "allowed_tool" in {s.name for s in registry.schemas()}
        assert "blocked_tool" not in {s.name for s in registry.schemas()}

        # 3. 验证调用 runtime.tools 时拦截禁用工具并生成错误信息
        call_bash = ToolCall(id="c1", name="bash", args={"command": "ls"})
        call_blocked = ToolCall(id="c2", name="blocked_tool", args={})
        res = await runtime.tools(state, (call_bash, call_blocked))
        from collections.abc import Sequence
        messages = res.get("messages")
        assert isinstance(messages, Sequence)
        assert any(
            isinstance(m, Message) and "未知工具：bash" in m.content for m in messages
        )
        assert any(
            isinstance(m, Message) and "未知工具：blocked_tool" in m.content for m in messages
        )

