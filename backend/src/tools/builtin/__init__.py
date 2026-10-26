"""内置工具：view_file / write_file / read_artifact / load_skill / bash。

设计文档：docs/04_tools/02_builtin_tools.md
"""

from tools.builtin.artifacts import ReadArtifactTool, TaskArtifacts, producer_candidates
from tools.builtin.assembly import ShellBinding, build_builtin_tools
from tools.builtin.files import FileLimits, ViewFileTool, WriteFileTool
from tools.builtin.paths import PathEscapeError, resolve_within_root
from tools.builtin.shell import BashTool, ShellResponse
from tools.builtin.skills import LoadableSkill, LoadSkillTool, SkillCatalog

__all__ = [
    "BashTool",
    "FileLimits",
    "LoadSkillTool",
    "LoadableSkill",
    "PathEscapeError",
    "ReadArtifactTool",
    "ShellBinding",
    "ShellResponse",
    "SkillCatalog",
    "TaskArtifacts",
    "ViewFileTool",
    "WriteFileTool",
    "build_builtin_tools",
    "producer_candidates",
    "resolve_within_root",
]
