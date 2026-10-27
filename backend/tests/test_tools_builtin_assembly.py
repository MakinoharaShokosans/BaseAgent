"""内置工具装配：缺依赖则不注册。出处：docs/04_tools/01_tool_system.md §10。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.skills import SkillLimits, SkillRegistry
from tools.builtin import FileLimits, ShellBinding, TaskArtifacts, build_builtin_tools
from tools.core import SidecarClient, ToolRegistry

FILES = FileLimits(view_max_lines=400, view_max_file_bytes=10**6, write_max_bytes=10**6)


def build(tmp_path: Path, *, shell: bool = False, skills: bool = False) -> list[str]:
    binding = (
        ShellBinding(
            SidecarClient("http://127.0.0.1:8002", timeout_sec=5),
            workspace_id="w",
            workspace_root=str(tmp_path),
            task_id="t",
            default_timeout_sec=60,
            max_timeout_sec=600,
        )
        if shell
        else None
    )
    catalog = (
        SkillRegistry.scan([], SkillLimits(200, 12, frozenset(), True, True)) if skills else None
    )
    tools = build_builtin_tools(
        workspace_root=tmp_path,
        files=FILES,
        artifacts=TaskArtifacts(),
        artifacts_root=tmp_path / "artifacts",
        grep_max_results=200,
        shell=binding,
        skills=catalog,
    )
    return [t.name for t in tools]


@pytest.mark.unit
def test_the_file_search_tools_and_read_artifact_are_always_there(tmp_path: Path) -> None:
    assert sorted(build(tmp_path)) == [
        "edit_file",
        "grep",
        "list_dir",
        "read_artifact",
        "view_file",
        "write_file",
    ]


@pytest.mark.unit
def test_bash_and_load_skill_appear_only_with_their_dependency(tmp_path: Path) -> None:
    assert "bash" not in build(tmp_path)
    assert "load_skill" not in build(tmp_path)
    assert "bash" in build(tmp_path, shell=True)
    assert "load_skill" in build(tmp_path, skills=True)


@pytest.mark.unit
def test_web_search_is_never_part_of_the_main_set(tmp_path: Path) -> None:
    assert "web_search" not in build(tmp_path, shell=True, skills=True)


@pytest.mark.unit
def test_every_built_tool_is_trusted_and_fits_the_privileged_registry(tmp_path: Path) -> None:
    tools = build_builtin_tools(
        workspace_root=tmp_path,
        files=FILES,
        artifacts=TaskArtifacts(),
        artifacts_root=tmp_path / "a",
        grep_max_results=200,
    )
    assert all(t.trust == "trusted" for t in tools)
    ToolRegistry(tools)  # 默认注册表（不接受不可信工具）不抛


@pytest.mark.unit
def test_the_exported_schemas_are_byte_identical_across_builds(tmp_path: Path) -> None:
    """工具 Schema 是前缀缓存的一部分：同一配置两次装配必须逐字节相同。"""

    def dump() -> str:
        tools = build_builtin_tools(
            workspace_root=tmp_path,
            files=FILES,
            artifacts=TaskArtifacts(),
            artifacts_root=tmp_path / "a",
            grep_max_results=200,
        )
        return json.dumps(
            [
                {"name": s.name, "description": s.description, "parameters": dict(s.parameters)}
                for s in ToolRegistry(tools).schemas()
            ],
            ensure_ascii=False,
        )

    assert dump() == dump()


@pytest.mark.unit
def test_descriptions_do_not_embed_task_specific_values(tmp_path: Path) -> None:
    tools = build_builtin_tools(
        workspace_root=tmp_path,
        files=FILES,
        artifacts=TaskArtifacts(),
        artifacts_root=tmp_path / "a",
        grep_max_results=200,
    )
    for tool in tools:
        assert str(tmp_path) not in tool.description
        assert str(tmp_path) not in json.dumps(dict(tool.parameters))


@pytest.mark.unit
def test_every_description_opens_with_a_self_contained_sentence(tmp_path: Path) -> None:
    """首行会进 planner 的能力清单。"""
    tools = build_builtin_tools(
        workspace_root=tmp_path,
        files=FILES,
        artifacts=TaskArtifacts(),
        artifacts_root=tmp_path / "a",
        grep_max_results=200,
    )
    for tool in tools:
        first = tool.description.split("。")[0]
        assert len(first) >= 8 and "\n" not in first


@pytest.mark.unit
async def test_read_artifact_trusts_what_the_built_in_tools_produced(tmp_path: Path) -> None:
    artifacts = TaskArtifacts()
    store = tmp_path / "artifacts"
    (store / "t").mkdir(parents=True)
    log = store / "t" / "obs_step_1_view_file.log"
    log.write_text("x\n")
    artifacts.bind({"obs_step_1_view_file": str(log), "obs_step_1_mcp_x": str(log)})
    tools = build_builtin_tools(
        workspace_root=tmp_path,
        files=FILES,
        artifacts=artifacts,
        artifacts_root=store,
        grep_max_results=10,
    )
    reader = next(t for t in tools if t.name == "read_artifact")
    own = await reader.invoke({"artifact_id": "obs_step_1_view_file"})
    foreign = await reader.invoke({"artifact_id": "obs_step_1_mcp_x"})
    assert (own.trust, foreign.trust) == ("trusted", "untrusted")
