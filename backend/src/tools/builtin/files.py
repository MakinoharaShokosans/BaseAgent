"""view_file 与 write_file。设计文档：04_tools/02_builtin_tools.md §2、§3。"""

from __future__ import annotations

import asyncio
import difflib
import io
import os
import re
import shutil
import stat
import tempfile
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_runtime.core import AgentTool, ToolResult
from tools.builtin.diagnostics import check_syntax
from tools.builtin.paths import PathEscapeError, int_arg, resolve_within_root, text_arg
from tools.builtin.textio import (
    SNIFF_BYTES,
    EolStats,
    clip_line,
    decode,
    eol_stats,
    show_whitespace,
    sniff_encoding,
)

_SNIFF_BYTES: Final = 8192
_MAX_CONTEXT: Final = 10
_PATTERN_MAX: Final = 500
_SCAN_BUDGET_SEC: Final = 10.0
_PREVIEW_LINES: Final = 40


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
        "带行号读取工作区内的文本文件。默认读开头，超出单次上限时按提示递增 start_line 继续读。"
        "start_line/end_line 指定行范围；start_line 为负数表示从文件末尾倒数"
        "（-50 读最后 50 行，适合日志）。"
        "pattern 在这个文件内按正则找行，只返回命中行（行号后是 :）及 context 行（行号后是 -），"
        "并给出命中总数，比通读大文件省得多；ignore_case、fixed 与 grep 相同。"
        "show_whitespace=true 把制表符显示为 →、行尾空格显示为 ·、"
        "每行末尾的换行显示为 ⏎（CRLF 为 ␍⏎），用来查缩进与换行问题。"
        "文件不是 UTF-8、带 BOM、换行不是 LF、或末尾没有换行时，会在开头给出一行说明。"
        "二进制文件与超大文件（未指定范围或 pattern 时）会返回失败说明。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的路径"},
            "start_line": {"type": "integer", "description": "起始行，从 1 开始；负数表示倒数"},
            "end_line": {"type": "integer", "description": "结束行（含）"},
            "pattern": {"type": "string", "description": "只返回匹配这个正则的行（及上下文）"},
            "context": {"type": "integer", "description": "pattern 命中行前后各带几行，0-10"},
            "ignore_case": {"type": "boolean"},
            "fixed": {"type": "boolean", "description": "把 pattern 当字面量"},
            "show_whitespace": {"type": "boolean", "description": "显形制表符、行尾空格与换行"},
        },
        "required": ["path"],
    }
    timeout_sec = 20.0
    idempotent = True

    def __init__(self, root: Path, limits: FileLimits) -> None:
        self._root = root.resolve()
        self._limits = limits

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        raw = text_arg(args, "path")
        if raw is None:
            return ToolResult.failure("缺少参数 path（字符串）")
        try:
            query = self._parse(args)
            path = resolve_within_root(self._root, raw)
        except re.error as exc:
            return ToolResult.failure(f"正则无法编译：{exc}")
        except (ValueError, PathEscapeError) as exc:
            return ToolResult.failure(str(exc))
        return await asyncio.to_thread(self._read, path, raw, query)

    @staticmethod
    def _parse(args: Mapping[str, Any]) -> _ViewQuery:
        start_arg = int_arg(args, "start_line")
        end = int_arg(args, "end_line")
        tail = -start_arg if start_arg is not None and start_arg < 0 else 0
        if tail:
            start = 1
            end = None
        else:
            start = 1 if start_arg is None else start_arg
            if start < 1:
                raise ValueError("start_line 必须从 1 开始（倒数请用负数）")
            if end is not None and end < start:
                raise ValueError("end_line 不能小于 start_line")
        pattern = text_arg(args, "pattern")
        regex = None
        if pattern:
            if len(pattern) > _PATTERN_MAX:
                raise ValueError(f"pattern 过长（上限 {_PATTERN_MAX} 字符）")
            flags = re.IGNORECASE if args.get("ignore_case") is True else 0
            regex = re.compile(re.escape(pattern) if args.get("fixed") is True else pattern, flags)
        context = max(0, min(int_arg(args, "context") or 0, _MAX_CONTEXT))
        explicit = args.get("start_line") is not None or args.get("end_line") is not None
        return _ViewQuery(
            start, end, tail, regex, context, args.get("show_whitespace") is True, explicit
        )

    def _read(self, path: Path, raw: str, q: _ViewQuery) -> ToolResult:
        try:
            if path.is_dir():
                return ToolResult.failure(f"{raw} 是目录，不是文件")
            size = path.stat().st_size
            if size > self._limits.view_max_file_bytes and not (q.explicit or q.regex or q.tail):
                return ToolResult.failure(
                    f"文件有 {size} 字节，超过全量读取上限；"
                    "请用 start_line 与 end_line 指定行范围，"
                    "或用 pattern 在文件内搜索"
                )
            with path.open("rb") as handle:
                sample = handle.read(SNIFF_BYTES)
            encoding = sniff_encoding(sample)
            if b"\x00" in sample[:_SNIFF_BYTES] and encoding != "utf-16":
                return ToolResult.failure(f"疑似二进制文件，大小 {size} 字节，不予显示")
            if size == 0:
                return ToolResult(ok=True, content="（空文件）")
            scan = _scan(path, encoding, q, self._limits.view_max_lines)
        except FileNotFoundError:
            return ToolResult.failure(f"文件不存在：{raw}")
        except OSError as exc:
            return ToolResult.failure(f"读取失败：{exc}")
        return _render_view(scan, raw, encoding, q, self._limits.view_max_lines)


@dataclass(frozen=True, slots=True)
class _ViewQuery:
    start: int
    end: int | None
    tail: int
    regex: re.Pattern[str] | None
    context: int
    whitespace: bool
    explicit: bool


@dataclass(slots=True)
class _Scan:
    #: (行号, 命中?, 正文, 行尾)；命中?只在 pattern 模式有意义
    rows: list[tuple[int, bool, str, str]]
    total: int
    stats: EolStats
    final_newline: bool
    matches: int = 0
    cut: bool = False
    #: 总行数未数完（大文件读到所需范围就停了）
    partial_total: bool = False


def _scan(path: Path, encoding: str, q: _ViewQuery, cap: int) -> _Scan:
    """一遍扫完：取所需行、数总行数与换行种类。行的切法与 grep 一致（见 textio）。"""
    lf = crlf = cr = 0
    total = 0
    last_term = ""
    rows: list[tuple[int, bool, str, str]] = []
    tail_buf: deque[tuple[int, bool, str, str]] = deque(maxlen=q.tail or 1)
    before: deque[tuple[int, str, str]] = deque(maxlen=max(q.context, 1))
    after_left = 0
    last_shown = 0
    matches = 0
    cut = False
    stop = None if q.end is None else min(q.end, q.start + cap - 1)
    if q.end is None and not q.regex and not q.tail:
        stop = q.start + cap - 1
    deadline = time.monotonic() + _SCAN_BUDGET_SEC
    partial = False
    with path.open("rb") as raw_handle:
        text = io.TextIOWrapper(raw_handle, encoding=encoding, errors="replace", newline="")
        for raw_line in text:
            total += 1
            body = raw_line.rstrip("\r\n")
            term = raw_line[len(body) :]
            last_term = term
            if term == "\r\n":
                crlf += 1
            elif term == "\n":
                lf += 1
            elif term == "\r":
                cr += 1
            if q.tail:
                tail_buf.append((total, False, body, term))
                continue
            if q.regex is not None:
                if total < q.start or (q.end is not None and total > q.end):
                    continue
                if q.regex.search(body[:LINE_CLIP_SCAN]):
                    matches += 1
                    if matches <= cap:
                        if rows and q.context and total - q.context - 1 > last_shown:
                            rows.append((0, False, "--", ""))
                        for n, b, t in before:
                            if n > last_shown:
                                rows.append((n, False, b, t))
                        rows.append((total, True, body, term))
                        last_shown = total
                        after_left = q.context
                        before.clear()
                    else:
                        cut = True
                elif after_left and matches <= cap:
                    rows.append((total, False, body, term))
                    last_shown = total
                    after_left -= 1
                elif q.context:
                    before.append((total, body, term))
                continue
            if q.start <= total <= (stop or total):
                rows.append((total, False, body, term))
            big = path.stat().st_size > _FULL_COUNT_BYTES or time.monotonic() > deadline
            if stop is not None and total >= stop and big:
                partial = True
                break
    if q.tail:
        rows = list(tail_buf)
    stats = EolStats(lf, crlf, cr)
    return _Scan(
        rows=rows,
        total=total,
        stats=stats,
        final_newline=bool(last_term),
        matches=matches,
        cut=cut,
        partial_total=partial,
    )


LINE_CLIP_SCAN: Final = 20000
_FULL_COUNT_BYTES: Final = 5_000_000


def _render_view(scan: _Scan, raw: str, encoding: str, q: _ViewQuery, cap: int) -> ToolResult:
    attrs: list[str] = []
    label = {
        "utf-8-sig": "UTF-8 BOM",
        "utf-16": "UTF-16",
        "gb18030": "GB18030（非 UTF-8，已按它解码）",
    }.get(encoding, "")
    if label:
        attrs.append(label)
    if scan.stats.style not in ("LF", "none"):
        attrs.append(scan.stats.describe())
    if scan.total and not scan.final_newline:
        attrs.append("末尾无换行")

    def fmt(n: int, hit: bool, body: str, term: str) -> str:
        if n == 0:
            return body  # 组间分隔符
        text = show_whitespace(clip_line(body), term) if q.whitespace else clip_line(body)
        if q.regex is not None:
            return f"{n:>6}{':' if hit else '-'}\t{text}"
        return f"{n:>6}\t{text}"

    if q.regex is not None:
        if not scan.matches:
            return ToolResult(
                ok=True, content=_with_header(raw, attrs, f"没有匹配（共 {scan.total} 行）")
            )
        body = "\n".join(fmt(*row) for row in scan.rows)
        note = f"[共 {scan.matches} 行匹配"
        if scan.cut:
            note += f"，只显示前 {cap} 行；请缩小范围或换更精确的 pattern"
        note += "]"
        return ToolResult(
            ok=True, content=_with_header(raw, attrs, f"{body}\n{note}"), is_truncated=scan.cut
        )

    if q.tail:
        shown = scan.rows
        body = "\n".join(fmt(*row) for row in shown)
        attrs.append(f"共 {scan.total} 行")
        return ToolResult(ok=True, content=_with_header(raw, attrs, body))

    if not scan.rows:
        return ToolResult.failure(f"起始行 {q.start} 超出文件总行数 {scan.total}")
    first, last = scan.rows[0][0], scan.rows[-1][0]
    wanted_end = scan.total if q.end is None else min(q.end, scan.total)
    truncated = last < wanted_end and (scan.partial_total or last < scan.total)
    if scan.partial_total:
        truncated = q.end is None or last < q.end
    body = "\n".join(fmt(*row) for row in scan.rows)
    if truncated:
        body += (
            f"\n[已显示第 {first}-{last} 行"
            + ("" if scan.partial_total else f"，共 {scan.total} 行")
            + f"；如需后续内容请将 start_line 设为 {last + 1}]"
        )
    if q.explicit and not scan.partial_total and not truncated:
        attrs.append(f"共 {scan.total} 行")
    return ToolResult(ok=True, content=_with_header(raw, attrs, body), is_truncated=truncated)


def _with_header(raw: str, attrs: list[str], body: str) -> str:
    return f"[{raw} · " + " · ".join(attrs) + f"]\n{body}" if attrs else body


class WriteFileTool(AgentTool):
    name = "write_file"
    description = (
        "整文件写入（覆盖）工作区内的文件，自动创建父目录。"
        "路径必须在工作区内；内容有大小上限，超限返回失败。"
        "覆盖已有文件前应先用 view_file 读过它。"
        "覆盖已有文件时默认保持它原来的换行风格与 BOM"
        "（原文件是 CRLF，content 里的 \\n 会按 CRLF 写入），"
        "避免整文件的换行被悄悄改掉；line_ending 可显式指定 lf 或 crlf。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的路径"},
            "content": {"type": "string", "description": "完整的新文件内容"},
            "line_ending": {
                "type": "string",
                "enum": ["keep", "lf", "crlf"],
                "description": "换行：keep（缺省）沿用原文件，lf、crlf 显式转换",
            },
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
        ending = text_arg(args, "line_ending") or "keep"
        if ending not in ("keep", "lf", "crlf"):
            return ToolResult.failure("line_ending 只能是 keep、lf 或 crlf")
        try:
            path = resolve_within_root(self._root, raw)
        except PathEscapeError as exc:
            return ToolResult.failure(str(exc))
        return await asyncio.to_thread(self._write, path, content, ending)

    def _write(self, path: Path, content: str, ending: str) -> ToolResult:
        rel = _rel(self._root, path)
        if path == self._root or path.is_dir():
            return ToolResult.failure(f"{rel} 是目录，不能作为文件写入")
        backed_up = False
        notes: list[str] = []
        try:
            mode = None
            encoding = "utf-8"
            if path.exists():
                mode = stat.S_IMODE(path.stat().st_mode)
                previous = decode(path.read_bytes())
                if previous.encoding == "utf-8-sig":
                    encoding = "utf-8-sig"
                    notes.append("保留 BOM")
                style = eol_stats(previous.text).style
                if ending == "keep" and style == "CRLF" and "\r\n" not in content:
                    content = content.replace("\n", "\r\n")
                    notes.append("按原文件的 CRLF 换行写入")
            if ending == "lf" and "\r" in content:
                content = content.replace("\r\n", "\n").replace("\r", "\n")
                notes.append("已转为 LF")
            elif ending == "crlf":
                content = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
                notes.append("已转为 CRLF")
            data = content.encode(encoding)
            if len(data) > self._limits.write_max_bytes:
                return ToolResult.failure(
                    f"内容 {len(data)} 字节，超过单次写入上限 {self._limits.write_max_bytes} 字节"
                )
            if path.exists():
                backed_up = self._backup(path, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._replace(path, data, mode)
        except _BackupError as exc:
            return ToolResult.failure(f"覆盖前备份失败，未写入：{exc}")
        except OSError as exc:
            return ToolResult.failure(f"写入失败：{exc}")
        if backed_up:
            notes.append("已备份原文件")
        note = "，" + "，".join(notes) if notes else ""
        return ToolResult(
            ok=True,
            content=f"已写入 {rel}（{len(content)} 字符，{len(data)} 字节{note}）"
            + _syntax_note(rel, content),
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
        "对工作区内已有文本文件做精确的局部替换。old_string 必须在文件中恰好出现一次"
        "（replace_all=true 时替换所有出现处）；"
        "多行文本直接写进 old_string/new_string，换行用 \\n。"
        "regex=true 时 old_string 是正则（^ 与 $ 按行匹配，. 默认不跨行，dotall=true 才跨行），"
        "new_string 里可用 \\1、\\g<名字> 引用捕获组。"
        "start_line/end_line 把匹配限制在这个行范围内，用来在多处相同文本里定位。"
        "edits 一次传多处修改（每项 old_string、new_string，"
        "可带 replace_all、regex、start_line、end_line），"
        "按顺序在内存里应用，任何一处失败就一处都不写；比连发多次 edit_file 更快也更安全。"
        "文件是 CRLF 换行时，你写的换行会自动按 CRLF 匹配与写入，BOM 保留；"
        "成功后返回改动的差异预览，直接用来核对。失败时会说明出现次数或给出最接近的片段。"
    )
    _EDIT_ITEM: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "old_string": {
                "type": "string",
                "description": "要被替换的原文（regex=true 时是正则）",
            },
            "new_string": {"type": "string", "description": "替换后的内容"},
            "replace_all": {"type": "boolean", "description": "替换所有出现处，缺省 false"},
            "regex": {"type": "boolean", "description": "old_string 是正则，缺省 false"},
            "dotall": {"type": "boolean", "description": "regex 模式下 . 也匹配换行"},
            "ignore_case": {"type": "boolean", "description": "regex 模式下忽略大小写"},
            "start_line": {"type": "integer", "description": "只在这一行之后（含）匹配"},
            "end_line": {"type": "integer", "description": "只在这一行之前（含）匹配"},
        },
        "required": ["old_string", "new_string"],
    }
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对工作区根的路径"},
            **_EDIT_ITEM["properties"],
            "edits": {
                "type": "array",
                "items": _EDIT_ITEM,
                "description": "多处修改，原子应用；与顶层 old_string/new_string 二选一",
            },
        },
        "required": ["path"],
    }

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        raw = text_arg(args, "path")
        if raw is None:
            return ToolResult.failure("缺少参数 path（字符串）")
        batch = args.get("edits")
        if batch is not None:
            if not isinstance(batch, list) or not batch:
                return ToolResult.failure("edits 必须是非空数组")
            if "old_string" in args or "new_string" in args:
                return ToolResult.failure("edits 与顶层 old_string/new_string 只能给一个")
            items: list[Mapping[str, Any]] = []
            for i, item in enumerate(batch):
                if not isinstance(item, Mapping):
                    return ToolResult.failure(f"edits[{i}] 必须是对象")
                items.append(item)  # pyright: ignore[reportUnknownArgumentType]
        else:
            items = [args]
        try:
            specs = [
                _edit_spec(item, label=f"edits[{i}]" if batch is not None else "")
                for i, item in enumerate(items)
            ]
            path = resolve_within_root(self._root, raw)
        except (ValueError, PathEscapeError) as exc:
            return ToolResult.failure(str(exc))
        return await asyncio.to_thread(self._edit, path, specs)

    def _edit(self, path: Path, specs: Sequence[_EditSpec]) -> ToolResult:
        rel = _rel(self._root, path)
        try:
            if path.is_dir():
                return ToolResult.failure(f"{rel} 是目录，不是文件")
            decoded = decode(path.read_bytes())
        except FileNotFoundError:
            return ToolResult.failure(f"文件不存在：{rel}；新建文件请用 write_file")
        except OSError as exc:
            return ToolResult.failure(f"读取失败：{exc}")
        if decoded.encoding not in ("utf-8", "utf-8-sig") or decoded.lossy:
            return ToolResult.failure(
                f"{rel} 不是 UTF-8 文本文件（{decoded.label() or '无法解码'}），不能做文本替换"
            )

        stats = eol_stats(decoded.text)
        crlf = stats.style == "CRLF"
        work = (
            decoded.text.replace("\r\n", "\n") if crlf else decoded.text
        )  # 纯 CRLF 文件在内存里按 LF 处理
        before = work
        done = 0
        for index, spec in enumerate(specs):
            where = f"edits[{index}]：" if len(specs) > 1 else ""
            try:
                work, count = _apply(work, spec, crlf=crlf, rel=rel, stats=stats)
            except _EditError as exc:
                return ToolResult.failure(f"{where}{exc}")
            done += count
        if work == before:
            return ToolResult.failure("修改后内容与原文件完全相同，没有可做的修改")
        out_text = work.replace("\n", "\r\n") if crlf else work
        data = out_text.encode(decoded.encoding)
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
        notes = ["已备份原文件"] if backed_up else []
        if crlf:
            notes.append("按 CRLF 写入")
        if decoded.encoding == "utf-8-sig":
            notes.append("保留 BOM")
        head = f"已修改 {rel}：替换 {done} 处" + ("，" + "，".join(notes) if notes else "")
        return ToolResult(
            ok=True,
            content=f"{head}\n{_diff_preview(before, work)}" + _syntax_note(rel, out_text),
        )


class _EditError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _EditSpec:
    old: str
    new: str
    replace_all: bool
    regex: re.Pattern[str] | None
    start_line: int | None
    end_line: int | None


def _edit_spec(item: Mapping[str, Any], *, label: str) -> _EditSpec:
    where = f"{label}：" if label else ""
    old = text_arg(item, "old_string")
    new = text_arg(item, "new_string")
    if old is None or new is None:
        raise ValueError(f"{where}需要参数 old_string 与 new_string，且都是字符串")
    if not old:
        raise ValueError(f"{where}old_string 不能为空；新建文件请用 write_file")
    if old == new and item.get("regex") is not True:
        raise ValueError(f"{where}old_string 与 new_string 相同，没有可做的修改")
    regex = None
    if item.get("regex") is True:
        flags = re.MULTILINE
        if item.get("dotall") is True:
            flags |= re.DOTALL
        if item.get("ignore_case") is True:
            flags |= re.IGNORECASE
        try:
            regex = re.compile(old, flags)
        except re.error as exc:
            raise ValueError(f"{where}正则无法编译：{exc}") from exc
    start, end = int_arg(item, "start_line"), int_arg(item, "end_line")
    if start is not None and start < 1:
        raise ValueError(f"{where}start_line 必须从 1 开始")
    if start is not None and end is not None and end < start:
        raise ValueError(f"{where}end_line 不能小于 start_line")
    return _EditSpec(old, new, item.get("replace_all") is True, regex, start, end)


def _line_offsets(text: str, start: int | None, end: int | None) -> tuple[int, int]:
    """行范围（1 基，含）对应的字符区间 ``[lo, hi)``。"""
    lo = 0
    if start is not None and start > 1:
        index = -1
        for _ in range(start - 1):
            index = text.find("\n", index + 1)
            if index == -1:
                return len(text), len(text)
        lo = index + 1
    hi = len(text)
    if end is not None:
        index = lo - 1 if lo else -1
        index = -1
        for _ in range(end):
            index = text.find("\n", index + 1)
            if index == -1:
                break
        hi = len(text) if index == -1 else index + 1
    return lo, hi


def _apply(text: str, spec: _EditSpec, *, crlf: bool, rel: str, stats: EolStats) -> tuple[str, int]:
    old, new = spec.old, spec.new
    if crlf:  # 模型写的 \r\n 与 \n 都当作换行；内存里一律是 \n
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    lo, hi = _line_offsets(text, spec.start_line, spec.end_line)
    scope = text[lo:hi]
    where = (
        f"（限定在第 {spec.start_line or 1}-{spec.end_line or '末'} 行）"
        if spec.start_line or spec.end_line
        else ""
    )
    if spec.regex is not None:
        matches = list(spec.regex.finditer(scope))
        count = len(matches)
        if count == 0:
            raise _EditError(f"在 {rel} 中没有匹配这个正则的内容{where}：{spec.old[:120]}")
        if count > 1 and not spec.replace_all:
            lines = [text.count("\n", 0, lo + m.start()) + 1 for m in matches[:10]]
            raise _EditError(
                f"正则在 {rel} 中匹配了 {count} 处（第 {lines} 行）{where}，无法唯一定位；"
                "请收紧正则、用 start_line/end_line 限定，或设置 replace_all=true"
            )
        try:
            replaced = (
                spec.regex.sub(new, scope) if spec.replace_all else spec.regex.sub(new, scope, 1)
            )
        except (re.error, IndexError) as exc:
            raise _EditError(f"new_string 里的捕获组引用无效：{exc}") from exc
        return text[:lo] + replaced + text[hi:], count if spec.replace_all else 1

    count = scope.count(old)
    if count == 0:
        raise _EditError(_no_match(rel, text, old, where, stats))
    if count > 1 and not spec.replace_all:
        lines = [text.count("\n", 0, lo + i) + 1 for i in _positions(scope, old)]
        raise _EditError(
            f"old_string 在 {rel} 中出现了 {count} 次（第 {lines} 行）{where}，无法唯一定位；"
            "请补充前后文使其唯一，用 start_line/end_line 限定，或设置 replace_all=true"
        )
    replaced = scope.replace(old, new) if spec.replace_all else scope.replace(old, new, 1)
    return text[:lo] + replaced + text[hi:], count if spec.replace_all else 1


def _positions(text: str, needle: str) -> list[int]:
    found: list[int] = []
    start = 0
    while (index := text.find(needle, start)) != -1 and len(found) < 10:
        found.append(index)
        start = index + len(needle)
    return found


def _diff_preview(before: str, after: str) -> str:
    """改动的统一差异（前后各 2 行上下文），超长截断。行号是改后文件的行号。"""
    diff = list(
        difflib.unified_diff(
            before.split("\n"), after.split("\n"), lineterm="", n=2, fromfile="", tofile=""
        )
    )[2:]
    shown = diff[:_PREVIEW_LINES]
    more = len(diff) - len(shown)
    body = "\n".join(clip_line(line, 200) for line in shown)
    return body + (f"\n…（差异还有 {more} 行未显示）" if more > 0 else "")


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


def _no_match(rel: str, text: str, old: str, where: str, stats: EolStats) -> str:
    """零匹配：给出最接近的一行，帮助模型改正缩进、空白或拼写；换行混合时直接点明。"""
    first = old.strip().splitlines()[0].strip() if old.strip() else ""
    candidates = [line for line in text.splitlines() if line.strip()]
    close = difflib.get_close_matches(first, [c.strip() for c in candidates], n=1, cutoff=0.6)
    hint = ""
    if close:
        line_no = next(i for i, c in enumerate(candidates, 1) if c.strip() == close[0])
        shown = next(line for line in candidates if line.strip() == close[0])
        hint = f"。最接近的一行（约第 {_true_line(text, shown, line_no)} 行）：{shown[:200]}"
    if stats.style == "mixed":
        hint += (
            f"。注意文件的{stats.describe()}，跨行的 old_string 可能因此对不上，"
            "可改用 regex 或缩小到单行"
        )
    return f"在 {rel} 中没有找到 old_string{where}（需逐字一致，包括缩进与空白）{hint}"


def _true_line(text: str, shown: str, fallback: int) -> int:
    index = text.find(shown)
    return text.count("\n", 0, index) + 1 if index != -1 else fallback
