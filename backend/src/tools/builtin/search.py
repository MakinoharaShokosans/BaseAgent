"""list_dir 与 grep：只读、结构化、不经过 shell。设计文档：04_tools/02_builtin_tools.md §6。

权限判定因此是确定的（工具名即只读），不依赖对命令文本的正则分类。
"""

from __future__ import annotations

import asyncio
import bisect
import os
import re
import time
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_runtime.core import AgentTool, ToolResult
from tools.builtin.paths import PathEscapeError, int_arg, resolve_within_root, text_arg
from tools.builtin.textio import clip_line, decode, glob_to_regex, split_lines

_SNIFF: Final = 8192
_LINE_CAP: Final = 400
_MAX_PATTERN: Final = 500
_BUDGET_SEC: Final = 10.0
_MAX_CONTEXT: Final = 10


def _visible(entry: os.DirEntry[str], ignored: Collection[str]) -> bool:
    if entry.name in ignored:
        return False
    return not (entry.name.startswith(".") and entry.name not in {".github", ".env.example"})


class ListDirTool(AgentTool):
    name = "list_dir"
    description = (
        "列出工作区内目录的结构（默认深度 2），自动忽略 .git、node_modules 等噪音目录。"
        "pattern 给通配符（如 **/*.test.ts、src/**/*.{ts,tsx}、*.md）时改为按文件名或路径找文件，"
        "平铺列出所有匹配的相对路径，不受 depth 限制，比拼 find 命令更稳。"
        "details=true 在文件后附大小。条目过多时会截断并提示。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的目录，缺省为根"},
            "depth": {
                "type": "integer",
                "description": "展开层数，1-6，缺省 2（给 pattern 时忽略）",
            },
            "pattern": {
                "type": "string",
                "description": "通配符：* 不跨目录，** 跨目录，? 单字符，{a,b} 二选一；"
                "不含 / 只匹配文件名，含 / 匹配相对 path 的整条路径",
            },
            "details": {"type": "boolean", "description": "附上文件大小"},
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
        pattern = text_arg(args, "pattern")
        details = args.get("details") is True
        if pattern:
            return await asyncio.to_thread(self._find, target, raw, pattern, details, cap)
        return await asyncio.to_thread(self._list, target, raw, depth, cap, details)

    def _list(self, target: Path, raw: str, depth: int, cap: int, details: bool) -> ToolResult:
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
                suffix = f"  ({_size(entry)})" if details and not is_dir else ""
                lines.append(f"{'  ' * level}{entry.name}{'/' if is_dir else ''}{suffix}")
                if is_dir and level + 1 < depth:
                    walk(Path(entry.path), level + 1)

        walk(target, 0)
        if not lines:
            return ToolResult(ok=True, content="（空目录）")
        body = "\n".join(lines)
        if state["cut"]:
            body += f"\n[已显示 {state['count']} 项，还有更多；请缩小 path 或减小 depth]"
        return ToolResult(ok=True, content=body, is_truncated=state["cut"])

    def _find(self, target: Path, raw: str, pattern: str, details: bool, cap: int) -> ToolResult:
        if not target.exists():
            return ToolResult.failure(f"目录不存在：{raw}")
        if not target.is_dir():
            return ToolResult.failure(f"{raw} 是文件，不是目录")
        matcher = glob_to_regex(pattern)
        found: list[str] = []
        total = 0
        deadline = time.monotonic() + _BUDGET_SEC
        stopped = ""
        for entry_path, rel in _walk_files(target, self._root, self._ignored):
            if time.monotonic() > deadline:
                stopped = "搜索超时，结果不完整"
                break
            if not matcher.fullmatch(rel):
                continue
            total += 1
            if len(found) < cap:
                suffix = f"  ({_size_of(entry_path)})" if details else ""
                found.append(f"{rel}{suffix}")
        if not found:
            return ToolResult(ok=True, content=f"没有文件匹配 {pattern}（在 {raw} 下）")
        body = "\n".join(found)
        cut = total > len(found) or bool(stopped)
        if total > len(found):
            body += (
                f"\n[共 {total} 个文件匹配，只显示前 {len(found)} 个；请收紧 pattern 或缩小 path]"
            )
        if stopped:
            body += f"\n[{stopped}]"
        return ToolResult(ok=True, content=body, is_truncated=cut)


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _size(entry: os.DirEntry[str]) -> str:
    try:
        return _human(entry.stat(follow_symlinks=False).st_size)
    except OSError:
        return "?"


def _size_of(path: Path) -> str:
    try:
        return _human(path.stat().st_size)
    except OSError:
        return "?"


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _walk_files(
    base: Path, root: Path, ignored: Collection[str], exclude: re.Pattern[str] | None = None
) -> Iterator[tuple[Path, str]]:
    """深度优先遍历 ``base`` 下的文件，产出（路径，相对 ``base`` 的 POSIX 路径）。

    跳过噪音目录、隐藏项与指向工作区外的链接；``exclude`` 命中的目录整棵不进。
    """
    if base.is_file():
        yield base, base.name
        return

    def walk(directory: Path) -> Iterator[tuple[Path, str]]:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if not _visible(entry, ignored):
                continue
            path = Path(entry.path)
            if entry.is_symlink() and not _inside(root, path):
                continue
            rel = path.relative_to(base).as_posix()
            if exclude is not None and exclude.fullmatch(rel if entry.is_file() else rel + "/x"):
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from walk(path)
            elif entry.is_file(follow_symlinks=True):
                yield path, rel

    yield from walk(base)


class GrepTool(AgentTool):
    name = "grep"
    description = (
        "在工作区内按正则搜索文本。output_mode：content（缺省）输出 路径:行号:内容，命中行用 :、"
        "上下文行用 -；files 只列有命中的文件，大范围摸底时先用它；count 列每个文件的命中数。"
        "glob 限定文件，如 *.py（只匹配文件名）、src/**/*.{ts,tsx}（含 / 时匹配相对路径）；"
        "exclude_glob 排除。context 前后各带几行，也可用 before、after 分别指定。"
        "multiline=true 让模式可以跨行（用 \\n 或 [\\s\\S] 匹配换行，^ 与 $ 仍按行），"
        "命中的每一行都输出；ignore_case 忽略大小写，fixed 按字面量。"
        "自动跳过噪音目录、二进制与超大文件；命中过多时截断，用 offset 翻下一页。"
        "行号与 view_file 完全一致，可直接拿去 view_file 或 edit_file 的 start_line。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则（fixed=true 时按字面量）"},
            "path": {"type": "string", "description": "相对工作区根的文件或目录，缺省为根"},
            "glob": {"type": "string", "description": "只搜索匹配的文件，如 *.py、src/**/*.ts"},
            "exclude_glob": {"type": "string", "description": "排除匹配的文件或目录，如 tests/**"},
            "output_mode": {"type": "string", "enum": ["content", "files", "count"]},
            "context": {"type": "integer", "description": "每个命中前后各带几行，0-10"},
            "before": {"type": "integer", "description": "只指定命中之前的行数，0-10"},
            "after": {"type": "integer", "description": "只指定命中之后的行数，0-10"},
            "ignore_case": {"type": "boolean"},
            "fixed": {"type": "boolean", "description": "把 pattern 当字面量"},
            "multiline": {"type": "boolean", "description": "模式可以跨行"},
            "max_results": {"type": "integer", "description": "最多返回的条数，不超过配置上限"},
            "offset": {"type": "integer", "description": "跳过前面这么多条，用于翻页"},
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
            spec = self._spec(args, pattern)
        except re.error as exc:
            return ToolResult.failure(f"正则无法编译：{exc}")
        except (ValueError, PathEscapeError) as exc:
            return ToolResult.failure(str(exc))
        return await asyncio.to_thread(self._search, target, raw, spec)

    def _spec(self, args: Mapping[str, Any], pattern: str) -> _GrepSpec:
        mode = text_arg(args, "output_mode") or "content"
        if mode not in ("content", "files", "count"):
            raise ValueError("output_mode 只能是 content、files 或 count")
        both = max(0, min(int_arg(args, "context") or 0, _MAX_CONTEXT))
        before = int_arg(args, "before")
        after = int_arg(args, "after")
        flags = re.IGNORECASE if args.get("ignore_case") is True else 0
        multiline = args.get("multiline") is True
        if multiline:
            flags |= re.MULTILINE
        regex = re.compile(re.escape(pattern) if args.get("fixed") is True else pattern, flags)
        glob, exclude = text_arg(args, "glob"), text_arg(args, "exclude_glob")
        offset = int_arg(args, "offset") or 0
        if offset < 0:
            raise ValueError("offset 不能为负")
        return _GrepSpec(
            regex=regex,
            mode=mode,
            before=both if before is None else max(0, min(before, _MAX_CONTEXT)),
            after=both if after is None else max(0, min(after, _MAX_CONTEXT)),
            multiline=multiline,
            include=glob_to_regex(glob) if glob else None,
            exclude=glob_to_regex(exclude) if exclude else None,
            cap=max(1, min(int_arg(args, "max_results") or self._max, self._max)),
            offset=offset,
        )

    def _search(self, target: Path, raw: str, spec: _GrepSpec) -> ToolResult:
        if not target.exists():
            return ToolResult.failure(f"路径不存在：{raw}")
        deadline = time.monotonic() + _BUDGET_SEC
        out: list[str] = []
        searched = matched_files = skipped = emitted = 0
        more = False
        timed_out = False
        for path, rel_target in _walk_files(target, self._root, self._ignored, spec.exclude):
            if time.monotonic() > deadline:
                timed_out = True
                break
            rel = path.relative_to(self._root).as_posix()
            if spec.include is not None and not (
                spec.include.fullmatch(rel_target) or spec.include.fullmatch(rel)
            ):
                continue
            searched += 1
            found = self._match_file(path, spec)
            if found is None:
                continue
            lines, spans = found
            matched_files += 1
            if spec.mode in ("files", "count"):
                if skipped < spec.offset:
                    skipped += 1
                    continue
                if emitted >= spec.cap:
                    more = True
                    break
                out.append(rel if spec.mode == "files" else f"{rel}:{len(spans)}")
                emitted += 1
                continue
            shown: set[int] = set()
            last = -2
            for first, final in spans:
                if skipped < spec.offset:
                    skipped += 1
                    continue
                if emitted >= spec.cap:
                    more = True
                    break
                lo, hi = max(0, first - spec.before), min(len(lines), final + 1 + spec.after)
                if shown and (spec.before or spec.after) and lo > last + 1:
                    out.append("--")
                for n in range(lo, hi):
                    if n in shown:
                        continue
                    shown.add(n)
                    hit = first <= n <= final
                    sep = ":" if hit else "-"
                    out.append(f"{rel}{sep}{n + 1}{sep}{clip_line(lines[n], _LINE_CAP)}")
                last = hi - 1
                emitted += 1
            if more:
                break
        if not emitted and not skipped:
            return ToolResult(ok=True, content=f"没有匹配（已搜索 {searched} 个文件）")
        if not emitted:
            return ToolResult(ok=True, content=f"offset={spec.offset} 已超出全部 {skipped} 条结果")
        noun = "个文件" if spec.mode != "content" else "处匹配"
        notes: list[str] = []
        if more:
            last = spec.offset + emitted
            notes.append(
                f"已显示第 {spec.offset + 1}-{last} {noun[0]}，还有更多；"
                f"翻页请设 offset={last}，或缩小 path、加 glob"
            )
        if timed_out:
            notes.append("搜索超时，结果不完整")
        if not notes:
            return ToolResult(ok=True, content="\n".join(out))
        body = "\n".join(out) + "\n[" + "；".join(notes) + "]"
        return ToolResult(ok=True, content=body, is_truncated=more or timed_out)

    def _match_file(
        self, path: Path, spec: _GrepSpec
    ) -> tuple[list[str], list[tuple[int, int]]] | None:
        """文件里的命中：（行列表，命中的行区间 [(首行, 末行)]，0 基）；没有命中返回 None。"""
        try:
            if path.stat().st_size > self._max_bytes:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:_SNIFF] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return None
        lines = split_lines(decode(data).text)
        spans: list[tuple[int, int]] = []
        if spec.multiline:
            text = "\n".join(lines)
            starts = [0]
            for line in lines[:-1]:
                starts.append(starts[-1] + len(line) + 1)
            for m in spec.regex.finditer(text):
                first = bisect.bisect_right(starts, m.start()) - 1
                end_at = max(m.end() - 1, m.start())
                final = bisect.bisect_right(starts, end_at) - 1
                spans.append((first, max(first, final)))
        else:
            spans = [
                (i, i) for i, line in enumerate(lines) if spec.regex.search(line[: _LINE_CAP * 5])
            ]
        return (lines, spans) if spans else None


@dataclass(frozen=True, slots=True)
class _GrepSpec:
    regex: re.Pattern[str]
    mode: str
    before: int
    after: int
    multiline: bool
    include: re.Pattern[str] | None
    exclude: re.Pattern[str] | None
    cap: int
    offset: int
