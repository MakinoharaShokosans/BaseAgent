"""web_search sidecar 的客户端：把检索结果转成证据。

长页面的正文由 sidecar 落盘，这里按产物路径读回来（只接受产物根目录之内的文件），再截断。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agent_runtime.subagents.research.models import Evidence
from tools.core import DependencyUnavailableError, SidecarClient

_QUERY = "/api/v1/search/query"


class SidecarSearchClient:
    def __init__(
        self, client: SidecarClient, artifacts_root: Path, *, max_source_chars: int
    ) -> None:
        self._client = client
        self._root = artifacts_root.resolve()
        self._max = max_source_chars

    async def search(self, query: str, limit: int, task_id: str) -> list[Evidence]:
        try:
            reply = await self._client.post_json(
                _QUERY, {"query": query, "max_results": limit, "fetch": True, "task_id": task_id}
            )
        except DependencyUnavailableError:
            return []
        results: list[dict[str, Any]] = [
            r for r in reply.body.get("results", []) if isinstance(r, dict)
        ]
        return [await self._evidence(r) for r in results]

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
