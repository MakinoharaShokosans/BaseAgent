"""view_file 与 write_file。设计文档：04_tools/02_builtin_tools.md §2、§3。"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_runtime.core import AgentTool, ToolResult
from tools.builtin.diagnostics import check_syntax
from tools.builtin.paths import PathEscapeError, int_arg, resolve_within_root, text_arg

_SNIFF_BYTES: Final = 8192


@dataclass(frozen=True, slots=True)
class FileLimits:
    view_max_lines: int
    view_max_file_bytes: int
    write_max_bytes: int


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def numbered(first: int, lines: list[str]) -> str:
    return "\n".join(f"{first + i:>6}\t{line}" for i, line in enumerate(lines))


@dataclass(frozen=True, slots=True)
class Page:
    text: str
    first: int
    last: int
    total: int
    truncated: bool


def read_page(lines_iter: Any, start: int, end: int | None, cap: int) -> Page | None:
    """流式取 ``[start, end]`` 行（至多 ``cap`` 行），同时数出总行数。``start`` 越界返回 None。"""
    stop = start + cap - 1 if end is None else min(end, start + cap - 1)
    kept: list[str] = []
    total = 0
    for total, raw in enumerate(lines_iter, 1):
        if start <= total <= stop:
            kept.append(raw.rstrip("\r\n"))
    if start > total:
        return Page("", start, start, total, False)
    last = start + len(kept) - 1
    wanted_end = total if end is None else min(end, total)
    return Page(numbered(start, kept), start, last, total, last < wanted_end)


def page_result(page: Page) -> ToolResult:
    if page.first > page.total:
        return ToolResult.failure(f"起始行 {page.first} 超出文件总行数 {page.total}")
    body = page.text
    if page.truncated:
        body += (
            f"\n[已显示第 {page.first}-{page.last} 行，共 {page.total} 行；"
            f"如需后续内容请将 start_line 设为 {page.last + 1}]"
        )
    return ToolResult(ok=True, content=body, is_truncated=page.truncated)


def parse_range(args: Mapping[str, Any]) -> tuple[int, int | None]:
    start = int_arg(args, "start_line")
    end = int_arg(args, "end_line")
    start = 1 if start is None else start
    if start < 1:
        raise ValueError("start_line 必须从 1 开始")
    if end is not None and end < start:
        raise ValueError("end_line 不能小于 start_line")
    return start, end


class ViewFileTool(AgentTool):
    name = "view_file"
    description = (
        "带行号读取工作区内文件的指定行范围。"
        "路径必须在工作区内；单次最多返回固定行数，超出时按提示递增 start_line 继续读。"
        "二进制文件与超大文件（未指定行范围时）会返回失败说明。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的路径"},
            "start_line": {"type": "integer", "description": "起始行，从 1 开始"},
            "end_line": {"type": "integer", "description": "结束行（含）"},
        },
        "required": ["path"],
    }
    timeout_sec = 15.0
    idempotent = True

    def __init__(self, root: Path, limits: FileLimits) -> None:
        self._root = root.resolve()
        self._limits = limits

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        raw = text_arg(args, "path")
        if raw is None:
            return ToolResult.failure("缺少参数 path（字符串）")
        try:
            start, end = parse_range(args)
            path = resolve_within_root(self._root, raw)
        except (ValueError, PathEscapeError) as exc:
            return ToolResult.failure(str(exc))
        explicit = args.get("start_line") is not None or args.get("end_line") is not None
        return await asyncio.to_thread(self._read, path, raw, start, end, explicit)

    def _read(
        self, path: Path, raw: str, start: int, end: int | None, explicit: bool
    ) -> ToolResult:
        try:
            if path.is_dir():
                return ToolResult.failure(f"{raw} 是目录，不是文件")
            size = path.stat().st_size
            if size > self._limits.view_max_file_bytes and not explicit:
                return ToolResult.failure(
                    f"文件有 {size} 字节，超过全量读取上限；请用 start_line 与 end_line 指定行范围"
                )
            with path.open("rb") as handle:
                if b"\x00" in handle.read(_SNIFF_BYTES):
                    return ToolResult.failure(f"疑似二进制文件，大小 {size} 字节，不予显示")
            if size == 0:
                return ToolResult(ok=True, content="（空文件）")
            with path.open("r", encoding="utf-8", errors="replace", newline=None) as text:
                page = read_page(text, start, end, self._limits.view_max_lines)
        except FileNotFoundError:
            return ToolResult.failure(f"文件不存在：{raw}")
        except OSError as exc:
            return ToolResult.failure(f"读取失败：{exc}")
        assert page is not None
        return page_result(page)


class WriteFileTool(AgentTool):
    name = "write_file"
    description = (
        "整文件写入（覆盖）工作区内的文件，自动创建父目录。"
        "路径必须在工作区内；内容有大小上限，超限返回失败。"
        "覆盖已有文件前应先用 view_file 读过它。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的路径"},
            "content": {"type": "string", "description": "完整的新文件内容"},
        },
        "required": ["path", "content"],
    }
    timeout_sec = 15.0
    idempotent = False

    def __init__(self, root: Path, limits: FileLimits, *, backup_dir: Path | None = None) -> None:
        self._root = root.resolve()
        self._limits = limits
        self._backup_dir = backup_dir

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        raw = text_arg(args, "path")
        content = text_arg(args, "content")
        if raw is None or content is None:
            return ToolResult.failure("需要参数 path 与 content，且都是字符串")
        data = content.encode("utf-8")
        if len(data) > self._limits.write_max_bytes:
            return ToolResult.failure(
                f"内容 {len(data)} 字节，超过单次写入上限 {self._limits.write_max_bytes} 字节"
            )
        try:
            path = resolve_within_root(self._root, raw)
        except PathEscapeError as exc:
            return ToolResult.failure(str(exc))
        return await asyncio.to_thread(self._write, path, data, len(content))

    def _write(self, path: Path, data: bytes, chars: int) -> ToolResult:
        rel = _rel(self._root, path)
        if path == self._root or path.is_dir():
            return ToolResult.failure(f"{rel} 是目录，不能作为文件写入")
        backed_up = False
        try:
            mode = None
            if path.exists():
                mode = stat.S_IMODE(path.stat().st_mode)
                backed_up = self._backup(path, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._replace(path, data, mode)
        except _BackupError as exc:
            return ToolResult.failure(f"覆盖前备份失败，未写入：{exc}")
        except OSError as exc:
            return ToolResult.failure(f"写入失败：{exc}")
        note = "，已备份原文件" if backed_up else ""
        return ToolResult(
            ok=True,
            content=f"已写入 {rel}（{chars} 字符，{len(data)} 字节{note}）"
            + _syntax_note(rel, data.decode("utf-8", errors="replace")),
        )

    def _backup(self, path: Path, rel: str) -> bool:
        if self._backup_dir is None:
            return False
        target = self._backup_dir / rel
        n = 0
        while target.exists():
            n += 1
            target = self._backup_dir / f"{rel}.{n}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        except OSError as exc:
            raise _BackupError(str(exc)) from exc
        return True

    @staticmethod
    def _replace(path: Path, data: bytes, mode: int | None) -> None:
        """先写同目录临时文件再 ``os.replace``：写到一半失败不会留下残缺的目标文件。"""
        fd, tmp_name = tempfile.mkstemp(prefix=".baseagent-", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            if mode is not None:
                tmp.chmod(mode)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


class _BackupError(Exception):
    pass


class EditFileTool(WriteFileTool):
    name = "edit_file"
    description = (
        "对工作区内已有文本文件做精确的局部替换：old_string 必须在文件中恰好出现一次"
        "（replace_all=true 时替换所有出现处）。比整文件覆盖省 Token，也不会碰到无关内容。"
        "失败时会说明出现次数或给出最接近的片段，据此补充上下文再试。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的路径"},
            "old_string": {"type": "string", "description": "要被替换的原文，需逐字一致"},
            "new_string": {"type": "string", "description": "替换后的内容"},
            "replace_all": {"type": "boolean", "description": "替换所有出现处，缺省 false"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        raw = text_arg(args, "path")
        old = text_arg(args, "old_string")
        new = text_arg(args, "new_string")
        if raw is None or old is None or new is None:
            return ToolResult.failure("需要参数 path、old_string、new_string，且都是字符串")
        if not old:
            return ToolResult.failure("old_string 不能为空；新建文件请用 write_file")
        if old == new:
            return ToolResult.failure("old_string 与 new_string 相同，没有可做的修改")
        replace_all = args.get("replace_all") is True
        try:
            path = resolve_within_root(self._root, raw)
        except PathEscapeError as exc:
            return ToolResult.failure(str(exc))
        return await asyncio.to_thread(self._edit, path, old, new, replace_all)

    def _edit(self, path: Path, old: str, new: str, replace_all: bool) -> ToolResult:
        rel = _rel(self._root, path)
        try:
            if path.is_dir():
                return ToolResult.failure(f"{rel} 是目录，不是文件")
            original = path.read_bytes()
            text = original.decode("utf-8")
        except FileNotFoundError:
            return ToolResult.failure(f"文件不存在：{rel}；新建文件请用 write_file")
        except UnicodeDecodeError:
            return ToolResult.failure(f"{rel} 不是 UTF-8 文本文件，不能做文本替换")
        except OSError as exc:
            return ToolResult.failure(f"读取失败：{exc}")

        crlf = "\r\n" in text and text.count("\r\n") >= text.count("\n") / 2
        if crlf and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")  # 保留文件的换行风格

        count = text.count(old)
        if count == 0:
            return ToolResult.failure(_no_match(rel, text, old))
        if count > 1 and not replace_all:
            lines = _match_lines(text, old)
            return ToolResult.failure(
                f"old_string 在 {rel} 中出现了 {count} 次（第 {lines} 行），无法唯一定位；"
                "请补充前后文使其唯一，或设置 replace_all=true"
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        data = updated.encode("utf-8")
        if len(data) > self._limits.write_max_bytes:
            return ToolResult.failure(f"修改后文件 {len(data)} 字节，超过上限")
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            backed_up = self._backup(path, rel)
            self._replace(path, data, mode)
        except _BackupError as exc:
            return ToolResult.failure(f"修改前备份失败，未写入：{exc}")
        except OSError as exc:
            return ToolResult.failure(f"写入失败：{exc}")
        done = count if replace_all else 1
        note = "，已备份原文件" if backed_up else ""
        return ToolResult(
            ok=True, content=f"已修改 {rel}：替换 {done} 处{note}" + _syntax_note(rel, updated)
        )


def _syntax_note(rel: str, content: str) -> str:
    """写入后的即时语法诊断：改坏了就在这次的观察值里直接告诉模型，不必等到跑测试才发现。"""
    problem = check_syntax(rel, content)
    return f"\n[语法检查] {rel} 现在有语法错误（{problem}），请立即修复。" if problem else ""


def _match_lines(text: str, needle: str) -> list[int]:
    lines, start = [], 0
    while (index := text.find(needle, start)) != -1 and len(lines) < 10:
        lines.append(text.count("\n", 0, index) + 1)
        start = index + len(needle)
    return lines


def _no_match(rel: str, text: str, old: str) -> str:
    """零匹配：给出最接近的一行，帮助模型改正缩进、空白或拼写。"""
    import difflib

    first = old.strip().splitlines()[0].strip() if old.strip() else ""
    candidates = [line for line in text.splitlines() if line.strip()]
    close = difflib.get_close_matches(first, [c.strip() for c in candidates], n=1, cutoff=0.6)
    hint = ""
    if close:
        line_no = next(i for i, c in enumerate(candidates, 1) if c.strip() == close[0])
        shown = next(line for line in candidates if line.strip() == close[0])
        hint = f"。最接近的一行（约第 {_true_line(text, shown, line_no)} 行）：{shown[:200]}"
    return f"在 {rel} 中没有找到 old_string（需逐字一致，包括缩进与空白）{hint}"


def _true_line(text: str, shown: str, fallback: int) -> int:
    index = text.find(shown)
    return text.count("\n", 0, index) + 1 if index != -1 else fallback
