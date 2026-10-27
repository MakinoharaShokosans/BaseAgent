"""list_dir 与 grep：只读、结构化、不经过 shell。设计文档：04_tools/02_builtin_tools.md §6。

权限判定因此是确定的（工具名即只读），不依赖对命令文本的正则分类。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import time
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, Final

from agent_runtime.core import AgentTool, ToolResult
from tools.builtin.paths import PathEscapeError, int_arg, resolve_within_root, text_arg

_SNIFF: Final = 8192
_LINE_CAP: Final = 400
_MAX_PATTERN: Final = 500
_BUDGET_SEC: Final = 10.0


def _visible(entry: os.DirEntry[str], ignored: Collection[str]) -> bool:
    if entry.name in ignored:
        return False
    return not (entry.name.startswith(".") and entry.name not in {".github", ".env.example"})


class ListDirTool(AgentTool):
    name = "list_dir"
    description = (
        "列出工作区内目录的结构（默认深度 2），自动忽略 .git、node_modules 等噪音目录。"
        "比反复拼 find 命令更稳；条目过多时会截断并提示。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的目录，缺省为根"},
            "depth": {"type": "integer", "description": "展开层数，1-6，缺省 2"},
            "max_entries": {"type": "integer", "description": "最多条目数，缺省使用配置上限"},
        },
    }
    timeout_sec = 15.0
    idempotent = True

    def __init__(self, root: Path, *, ignored_dirs: Collection[str], max_entries: int) -> None:
        self._root = root.resolve()
        self._ignored = frozenset(ignored_dirs)
        self._max = max_entries

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        try:
            raw = text_arg(args, "path") or "."
            depth = max(1, min(int_arg(args, "depth") or 2, 6))
            cap = max(1, min(int_arg(args, "max_entries") or self._max, self._max))
            target = resolve_within_root(self._root, raw)
        except (ValueError, PathEscapeError) as exc:
            return ToolResult.failure(str(exc))
        return await asyncio.to_thread(self._list, target, raw, depth, cap)

    def _list(self, target: Path, raw: str, depth: int, cap: int) -> ToolResult:
        if not target.exists():
            return ToolResult.failure(f"目录不存在：{raw}")
        if not target.is_dir():
            return ToolResult.failure(f"{raw} 是文件，不是目录")
        lines: list[str] = []
        state = {"count": 0, "cut": False}

        def walk(directory: Path, level: int) -> None:
            try:
                entries = sorted(
                    os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower())
                )
            except OSError as exc:
                lines.append(f"{'  ' * level}[无法读取：{exc.strerror}]")
                return
            for entry in entries:
                if not _visible(entry, self._ignored):
                    continue
                if state["count"] >= cap:
                    state["cut"] = True
                    return
                is_dir = entry.is_dir(follow_symlinks=False)
                if entry.is_symlink() and not _inside(self._root, Path(entry.path)):
                    continue  # 指向工作区外的链接不展示
                state["count"] += 1
                lines.append(f"{'  ' * level}{entry.name}{'/' if is_dir else ''}")
                if is_dir and level + 1 < depth:
                    walk(Path(entry.path), level + 1)

        walk(target, 0)
        if not lines:
            return ToolResult(ok=True, content="（空目录）")
        body = "\n".join(lines)
        if state["cut"]:
            body += f"\n[已显示 {state['count']} 项，还有更多；请缩小 path 或减小 depth]"
        return ToolResult(ok=True, content=body, is_truncated=state["cut"])


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


class GrepTool(AgentTool):
    name = "grep"
    description = (
        "在工作区内按正则搜索文本，输出 路径:行号:内容。可用 glob 限定文件（如 *.py）、"
        "context 带上下文行、ignore_case 忽略大小写、fixed 按字面量匹配。"
        "自动跳过噪音目录与二进制文件；命中过多时截断。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则（fixed=true 时按字面量）"},
            "path": {"type": "string", "description": "相对工作区根的文件或目录，缺省为根"},
            "glob": {"type": "string", "description": "只搜索匹配该通配的文件名，如 *.py"},
            "context": {"type": "integer", "description": "每个命中前后各带几行，0-5"},
            "ignore_case": {"type": "boolean"},
            "fixed": {"type": "boolean", "description": "把 pattern 当字面量"},
            "max_results": {"type": "integer", "description": "最多命中行数，不超过配置上限"},
        },
        "required": ["pattern"],
    }
    timeout_sec = 20.0
    idempotent = True

    def __init__(
        self,
        root: Path,
        *,
        ignored_dirs: Collection[str],
        max_results: int,
        max_file_bytes: int,
    ) -> None:
        self._root = root.resolve()
        self._ignored = frozenset(ignored_dirs)
        self._max = max_results
        self._max_bytes = max_file_bytes

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        pattern = text_arg(args, "pattern")
        if not pattern:
            return ToolResult.failure("缺少参数 pattern（非空字符串）")
        if len(pattern) > _MAX_PATTERN:
            return ToolResult.failure(f"pattern 过长（上限 {_MAX_PATTERN} 字符）")
        try:
            raw = text_arg(args, "path") or "."
            target = resolve_within_root(self._root, raw)
            context = max(0, min(int_arg(args, "context") or 0, 5))
            cap = max(1, min(int_arg(args, "max_results") or self._max, self._max))
            flags = re.IGNORECASE if args.get("ignore_case") is True else 0
            regex = re.compile(re.escape(pattern) if args.get("fixed") is True else pattern, flags)
        except re.error as exc:
            return ToolResult.failure(f"正则无法编译：{exc}")
        except (ValueError, PathEscapeError) as exc:
            return ToolResult.failure(str(exc))
        glob = text_arg(args, "glob")
        return await asyncio.to_thread(self._search, target, raw, regex, glob, context, cap)

    def _search(
        self,
        target: Path,
        raw: str,
        regex: re.Pattern[str],
        glob: str | None,
        context: int,
        cap: int,
    ) -> ToolResult:
        if not target.exists():
            return ToolResult.failure(f"路径不存在：{raw}")
        deadline = time.monotonic() + _BUDGET_SEC
        out: list[str] = []
        hits = 0
        files = 0
        stopped = ""
        for file in self._files(target, glob):
            if time.monotonic() > deadline:
                stopped = "搜索超时，结果不完整"
                break
            files += 1
            found, hits_in_file = self._grep_file(file, regex, context, cap - hits)
            out += found
            hits += hits_in_file
            if hits >= cap:
                stopped = f"已达到 {cap} 条命中上限，结果被截断"
                break
        if not hits:
            return ToolResult(ok=True, content=f"没有匹配（已搜索 {files} 个文件）")
        body = "\n".join(out)
        if stopped:
            body += f"\n[{stopped}；请缩小 path 或加 glob]"
        return ToolResult(ok=True, content=body, is_truncated=bool(stopped))

    def _files(self, target: Path, glob: str | None) -> Any:
        if target.is_file():
            yield target
            return
        stack = [target]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name, reverse=True)
            except OSError:
                continue
            for entry in entries:
                if not _visible(entry, self._ignored):
                    continue
                path = Path(entry.path)
                if entry.is_symlink() and not _inside(self._root, path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=True) and (
                    glob is None or fnmatch.fnmatch(entry.name, glob)
                ):
                    yield path

    def _grep_file(
        self, path: Path, regex: re.Pattern[str], context: int, budget: int
    ) -> tuple[list[str], int]:
        rel = path.relative_to(self._root).as_posix()
        try:
            if path.stat().st_size > self._max_bytes:
                return [], 0
            with path.open("rb") as handle:
                if b"\x00" in handle.read(_SNIFF):
                    return [], 0
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return [], 0
        matched = [i for i, line in enumerate(lines) if regex.search(line[: _LINE_CAP * 5])]
        out: list[str] = []
        shown: set[int] = set()
        taken = 0
        for index in matched[:budget]:
            taken += 1
            lo, hi = max(0, index - context), min(len(lines), index + context + 1)
            if out and context and lo > max(shown, default=-2) + 1:
                out.append("--")
            for n in range(lo, hi):
                if n in shown:
                    continue
                shown.add(n)
                sep = ":" if n == index or n in matched else "-"
                out.append(f"{rel}{sep}{n + 1}{sep}{lines[n][:_LINE_CAP]}")
        return out, taken
