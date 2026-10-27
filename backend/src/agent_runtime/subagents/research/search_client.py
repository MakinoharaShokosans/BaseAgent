"""web_search sidecar 的客户端：先只查摘要，再抓取挑中的页面。

分两步是为了省时间：每个检索词都抓全部结果页，再去重留 5 个，大半的抓取是白费的。
sidecar 报告的错误（被反爬拦截、全部后端失败）原样带回，不再和“搜到 0 条”混在一起。
长页面的正文由 sidecar 落盘，这里按产物路径读回来（只接受产物根目录之内的文件），再截断。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_runtime.subagents.research.models import Evidence
from tools.core import DependencyUnavailableError, SidecarClient

_QUERY = "/api/v1/search/query"
_FETCH = "/api/v1/search/fetch"
_FETCH_BATCH = 10  # sidecar 单次请求的上限


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """一次检索：摘要级的命中（``status`` 为 ``SNIPPET``，``text`` 是摘要）与失败原因。

    ``error`` 非空表示搜索源出了问题（被拦截、服务不可用），与 ``hits`` 为空但没有错误不同。
    """

    hits: list[Evidence] = field(default_factory=list[Evidence])
    error: str = ""


class SidecarSearchClient:
    def __init__(
        self, client: SidecarClient, artifacts_root: Path, *, max_source_chars: int
    ) -> None:
        self._client = client
        self._root = artifacts_root.resolve()
        self._max = max_source_chars

    async def search(self, query: str, limit: int, task_id: str) -> SearchOutcome:
        try:
            reply = await self._client.post_json(
                _QUERY, {"query": query, "max_results": limit, "fetch": False, "task_id": task_id}
            )
        except DependencyUnavailableError:
            return SearchOutcome(error="搜索服务不可用")
        hits = [
            Evidence(
                str(r.get("url", "")), str(r.get("title", "")), str(r.get("snippet", "")), "SNIPPET"
            )
            for r in reply.body.get("results", [])
            if isinstance(r, dict) and r.get("url")
        ]
        return SearchOutcome(hits, str(reply.body.get("error") or ""))

    async def fetch(self, hits: Sequence[Evidence], task_id: str) -> list[Evidence]:
        out: list[Evidence] = []
        for start in range(0, len(hits), _FETCH_BATCH):
            batch = hits[start : start + _FETCH_BATCH]
            try:
                reply = await self._client.post_json(
                    _FETCH,
                    {
                        "items": [
                            {"url": h.url, "title": h.title[:500], "snippet": h.text[:2000]}
                            for h in batch
                        ],
                        "task_id": task_id,
                    },
                )
            except DependencyUnavailableError:
                out += [Evidence(h.url, h.title, "", "ERROR") for h in batch]
                continue
            results: list[dict[str, Any]] = [
                r for r in reply.body.get("results", []) if isinstance(r, dict)
            ]
            out += [await self._evidence(r) for r in results]
        return out

    async def _evidence(self, item: dict[str, Any]) -> Evidence:
        status = str(item.get("status", "ERROR"))
        text = str(item.get("preview") or item.get("snippet") or "")
        artifact = item.get("artifact_path")
        artifact_id = ""
        if status == "OK" and isinstance(artifact, str):
            path = Path(artifact).resolve()  # noqa: ASYNC240
            if path.is_relative_to(self._root):
                text = await asyncio.to_thread(_read, path, self._max) or text
                artifact_id = path.stem
        if status == "SNIPPET_ONLY" or status == "BLOCKED":
            status = "SNIPPET"  # 只有搜索摘要，不当作正文来源
        return Evidence(
            str(item.get("url", "")), str(item.get("title", "")), text, status, artifact_id
        )


def _read(path: Path, limit: int) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""
