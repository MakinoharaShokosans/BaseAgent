"""read_artifact：取回被裁剪的完整输出。设计文档：04_tools/02_builtin_tools.md §4。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, Final

from agent_runtime.core import AgentTool, ToolResult, Trust
from tools.builtin.files import FileLimits, numbered, page_result, parse_range, read_page

_HANDLE_PREFIX: Final = "artifact://"
_PRODUCER: Final = re.compile(r"^(?:obs_)?step_\d+_(?P<tail>.+)$")
_SUFFIX: Final = re.compile(r"_\d+$")
_UNSAFE: Final = re.compile(r"[^A-Za-z0-9_.-]")
_MATCH_LINE_CAP: Final = 2000
_LISTED_IDS: Final = 10
_MAX_SEARCH_ARTIFACTS: Final = 10
_MAX_SEARCH_HITS_PER_ARTIFACT: Final = 3


class TaskArtifacts:
    """本任务登记过的产物（``id -> 路径``）。

    工具看不到 ``AgentState``，所以由 ``tool_runner`` 每次进入时把状态里的表整体推进来，
    推送是**替换**而不是合并：别的任务的产物不会残留。
    """

    def __init__(self) -> None:
        self._paths: Mapping[str, str] = {}

    def bind(self, artifacts: Mapping[str, str]) -> None:
        self._paths = dict(artifacts)

    def path_of(self, artifact_id: str) -> str | None:
        return self._paths.get(artifact_id)

    def ids(self) -> list[str]:
        return sorted(self._paths)


def producer_candidates(artifact_id: str) -> tuple[str, ...]:
    """从句柄推出产生它的工具名。重名后缀 ``_2`` 与工具名里的数字无法区分，两种读法都给出。"""
    match = _PRODUCER.match(artifact_id)
    if match is None:
        return ()
    tail = match["tail"]
    stripped = _SUFFIX.sub("", tail)
    return (tail,) if stripped in ("", tail) else (tail, stripped)


class ReadArtifactTool(AgentTool):
    name = "read_artifact"
    description = (
        "读取或检索本任务中被裁剪的输出产物。支持三种模式："
        "1) 不传参数：列出当前任务所有产物清单；"
        "2) 传入 query：在当前任务所有产物日志中全局搜索关键词并返回匹配列表；"
        "3) 传入 artifact_id：读取指定产物的详细内容（支持 start_line/end_line 分页与 grep 过滤）。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string", "description": "产物 id，可带 artifact:// 前缀"},
            "query": {
                "type": "string",
                "description": (
                    "跨产物搜索关键词或正则；给出时，在当前任务所有产物日志中搜索该关键词，"
                    "返回匹配的产物清单与命中行预览"
                ),
            },
            "start_line": {"type": "integer", "description": "起始行，从 1 开始"},
            "end_line": {"type": "integer", "description": "结束行（含）"},
            "grep": {"type": "string", "description": "正则；给出时只返回匹配行（忽略行范围）"},
        },
        "required": [],
    }
    timeout_sec = 15.0
    idempotent = True

    def __init__(
        self,
        artifacts: TaskArtifacts,
        root: Path,
        limits: FileLimits,
        *,
        grep_max_results: int,
        trusted_producers: Collection[str],
    ) -> None:
        self._artifacts = artifacts
        self._root = root.resolve()
        self._limits = limits
        self._grep_max = grep_max_results
        self._trusted = frozenset(_UNSAFE.sub("_", n) for n in trusted_producers)

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        raw_id = args.get("artifact_id")
        if raw_id is not None and not isinstance(raw_id, str):
            return ToolResult.failure("artifact_id 必须是字符串")
        raw_query = args.get("query")
        if raw_query is not None and not isinstance(raw_query, str):
            return ToolResult.failure("query 必须是字符串")

        aid_str = raw_id.strip() if raw_id is not None else ""
        query_str = raw_query.strip() if raw_query is not None else ""

        # 模式 A：清单发现（artifact_id 与 query 均为空）
        if not aid_str and not query_str:
            return await asyncio.to_thread(self._list_catalog)

        # 模式 B：跨产物全局搜索（提供了 query 且 artifact_id 为空）
        if not aid_str and query_str:
            try:
                regex = re.compile(query_str, re.IGNORECASE)
            except re.error as exc:
                return ToolResult.failure(f"query 不是合法的正则：{exc}")
            return await asyncio.to_thread(self._search_catalog, regex)

        # 模式 C：指定产物精准读取（提供了 artifact_id）
        artifact_id = aid_str.removeprefix(_HANDLE_PREFIX)
        recorded = self._artifacts.path_of(artifact_id)
        if recorded is None:
            known = ", ".join(self._artifacts.ids()[:_LISTED_IDS]) or "（本任务还没有产物）"
            return ToolResult.failure(f"没有这个产物：{artifact_id}。已有：{known}")
        pattern = args.get("grep")
        if pattern is not None and not isinstance(pattern, str):
            return ToolResult.failure("grep 必须是字符串")
        try:
            start, end = parse_range(args)
            regex = re.compile(pattern) if pattern else None
        except re.error as exc:
            return ToolResult.failure(f"grep 不是合法的正则：{exc}")
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        result = await asyncio.to_thread(self._read, artifact_id, recorded, start, end, regex)
        return result.model_copy(update={"trust": self._trust_of(artifact_id)})

    def _list_catalog(self) -> ToolResult:
        ids = self._artifacts.ids()
        if not ids:
            return ToolResult(ok=True, content="（本任务还没有产物）")
        lines: list[str] = [f"本任务产物清单（共 {len(ids)} 个）："]
        all_trusted = True
        for aid in ids:
            if self._trust_of(aid) != "trusted":
                all_trusted = False
            recorded = self._artifacts.path_of(aid)
            if recorded is None:
                lines.append(f"- artifact://{aid} (路径丢失)")
                continue
            try:
                path = Path(recorded).resolve()
            except (OSError, ValueError, RuntimeError):
                lines.append(f"- artifact://{aid} (不可读)")
                continue
            if not path.is_relative_to(self._root):
                lines.append(f"- artifact://{aid} (路径越界已拒绝)")
                continue
            try:
                size = path.stat().st_size
                preview = ""
                with path.open("r", encoding="utf-8", errors="replace", newline=None) as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped:
                            preview = " ".join(stripped.split())[:80]
                            break
                if preview:
                    lines.append(f"- artifact://{aid} ({size} 字节): {preview}")
                else:
                    lines.append(f"- artifact://{aid} ({size} 字节)")
            except OSError:
                lines.append(f"- artifact://{aid} (无法读取)")
        return ToolResult(
            ok=True,
            content="\n".join(lines),
            trust="trusted" if all_trusted else "untrusted",
        )

    def _search_catalog(self, regex: re.Pattern[str]) -> ToolResult:
        ids = self._artifacts.ids()
        if not ids:
            return ToolResult(ok=True, content="未在任何产物中找到匹配项")
        matches: list[tuple[str, list[tuple[int, str]]]] = []
        more_artifacts = False
        all_trusted = True
        for aid in ids:
            recorded = self._artifacts.path_of(aid)
            if recorded is None:
                continue
            try:
                path = Path(recorded).resolve()
            except (OSError, ValueError, RuntimeError):
                continue
            if not path.is_relative_to(self._root):
                continue
            hits: list[tuple[int, str]] = []
            try:
                with path.open("r", encoding="utf-8", errors="replace", newline=None) as f:
                    for lineno, line in enumerate(f, 1):
                        if regex.search(line[:_MATCH_LINE_CAP]):
                            hits.append((lineno, line.rstrip("\r\n")))
                            if len(hits) >= _MAX_SEARCH_HITS_PER_ARTIFACT:
                                break
            except OSError:
                continue
            if hits:
                if len(matches) >= _MAX_SEARCH_ARTIFACTS:
                    more_artifacts = True
                    break
                if self._trust_of(aid) != "trusted":
                    all_trusted = False
                matches.append((aid, hits))
        if not matches:
            return ToolResult(ok=True, content="未在任何产物中找到匹配项")
        blocks: list[str] = []
        for aid, hits in matches:
            block = f"### artifact://{aid}\n" + "\n".join(numbered(n, [line]) for n, line in hits)
            blocks.append(block)
        body = "\n\n".join(blocks)
        if more_artifacts:
            body += (
                f"\n\n[匹配产物超过上限 {_MAX_SEARCH_ARTIFACTS} 个，"
                f"只显示前 {_MAX_SEARCH_ARTIFACTS} 个；请收窄 query]"
            )
        return ToolResult(
            ok=True,
            content=body,
            is_truncated=more_artifacts,
            trust="trusted" if all_trusted else "untrusted",
        )

    def _trust_of(self, artifact_id: str) -> Trust:
        candidates = producer_candidates(artifact_id)
        # 认不出来源、或任一种读法不在可信名单里：取最保守值
        return (
            "trusted" if candidates and all(c in self._trusted for c in candidates) else "untrusted"
        )

    def _read(
        self,
        artifact_id: str,
        recorded: str,
        start: int,
        end: int | None,
        regex: re.Pattern[str] | None,
    ) -> ToolResult:
        try:
            path = Path(recorded).resolve()
        except (OSError, ValueError, RuntimeError):
            return ToolResult.failure(f"产物不可读：{artifact_id}")
        if not path.is_relative_to(self._root):
            return ToolResult.failure(f"产物不在产物目录内，已拒绝：{artifact_id}")
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline=None) as text:
                if regex is not None:
                    return self._grep(text, regex)
                page = read_page(text, start, end, self._limits.view_max_lines)
        except OSError:
            return ToolResult.failure(f"产物文件无法读取（可能已被清理）：{artifact_id}")
        assert page is not None
        return page_result(page)

    def _grep(self, text: Any, regex: re.Pattern[str]) -> ToolResult:
        hits: list[tuple[int, str]] = []
        more = False
        for number, line in enumerate(text, 1):
            if regex.search(line[:_MATCH_LINE_CAP]):
                if len(hits) >= self._grep_max:
                    more = True
                    break
                hits.append((number, line.rstrip("\r\n")))
        if not hits:
            return ToolResult(ok=True, content="没有匹配的行")
        body = "\n".join(numbered(n, [line]) for n, line in hits)
        if more:
            body += (
                f"\n[匹配行超过上限 {self._grep_max}，只显示了前 {self._grep_max} 条；请收窄 grep]"
            )
        return ToolResult(ok=True, content=body, is_truncated=more)
