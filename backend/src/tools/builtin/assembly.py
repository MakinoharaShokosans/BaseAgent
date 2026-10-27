"""内置工具装配。设计文档：04_tools/01_tool_system.md §10。

缺少某个依赖时对应工具不注册：模型根本看不到它，也就不会误调用一个不存在的能力。
刻意不含 ``web_search``：外部信息的唯一入口是研究子智能体。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from agent_runtime.core import AgentTool
from tools.builtin.artifacts import ReadArtifactTool, TaskArtifacts
from tools.builtin.files import EditFileTool, FileLimits, ViewFileTool, WriteFileTool
from tools.builtin.search import GrepTool, ListDirTool
from tools.builtin.shell import BashTool
from tools.builtin.skills import LoadSkillTool, SkillCatalog
from tools.core import SidecarClient


@dataclass(frozen=True, slots=True)
class ShellBinding:
    """bash 需要的任务上下文：客户端由装配层创建并在任务结束时关闭。"""

    client: SidecarClient
    workspace_id: str
    workspace_root: str
    task_id: str
    default_timeout_sec: float
    max_timeout_sec: float


def build_builtin_tools(
    *,
    workspace_root: Path,
    files: FileLimits,
    artifacts: TaskArtifacts,
    artifacts_root: Path,
    grep_max_results: int,
    backup_dir: Path | None = None,
    shell: ShellBinding | None = None,
    skills: SkillCatalog | None = None,
    ignored_dirs: Collection[str] = (),
    list_dir_max_entries: int = 200,
) -> list[AgentTool]:
    tools: list[AgentTool] = [
        ViewFileTool(workspace_root, files),
        WriteFileTool(workspace_root, files, backup_dir=backup_dir),
        EditFileTool(workspace_root, files, backup_dir=backup_dir),
        ListDirTool(workspace_root, ignored_dirs=ignored_dirs, max_entries=list_dir_max_entries),
        GrepTool(
            workspace_root,
            ignored_dirs=ignored_dirs,
            max_results=grep_max_results,
            max_file_bytes=files.view_max_file_bytes,
        ),
    ]
    if shell is not None:
        tools.append(
            BashTool(
                shell.client,
                workspace_id=shell.workspace_id,
                workspace_root=shell.workspace_root,
                task_id=shell.task_id,
                default_timeout_sec=shell.default_timeout_sec,
                max_timeout_sec=shell.max_timeout_sec,
            )
        )
    if skills is not None:
        tools.append(LoadSkillTool(skills))
    # 产物的来源信任由生成它的工具决定：这里装出来的都是可信工具
    tools.append(
        ReadArtifactTool(
            artifacts,
            artifacts_root,
            files,
            grep_max_results=grep_max_results,
            trusted_producers=[t.name for t in tools],
        )
    )
    return tools
