"""研究子智能体的 sidecar 客户端：先查摘要，再抓取；搜索错误原样带回。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runtime.subagents.research.models import Evidence
from agent_runtime.subagents.research.search_client import SidecarSearchClient
from tools.core import DependencyUnavailableError


class FakeSidecar:
    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, path: str, body: dict[str, Any]) -> Any:
        self.posts.append((path, body))
        reply = self.replies[path]
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(body=reply)


def client(tmp_path: Path, replies: dict[str, Any]) -> tuple[SidecarSearchClient, FakeSidecar]:
    sidecar = FakeSidecar(replies)
    return SidecarSearchClient(sidecar, tmp_path, max_source_chars=100), sidecar  # type: ignore[arg-type]


@pytest.mark.unit
async def test_search_asks_for_snippets_only_and_returns_summary_level_hits(tmp_path: Path) -> None:
    reply = {
        "results": [
            {
                "url": "https://a.example/x",
                "title": "A",
                "snippet": "摘要",
                "status": "SNIPPET_ONLY",
            },
            {"url": "", "title": "no url", "snippet": ""},
        ],
        "error": None,
    }
    c, sidecar = client(tmp_path, {"/api/v1/search/query": reply})
    outcome = await c.search("q", 8, "t1")
    assert sidecar.posts[0][1]["fetch"] is False
    assert outcome.error == "" and outcome.hits == [
        Evidence("https://a.example/x", "A", "摘要", "SNIPPET")
    ]


@pytest.mark.unit
async def test_a_search_error_comes_back_instead_of_an_empty_success(tmp_path: Path) -> None:
    c, _ = client(tmp_path, {"/api/v1/search/query": {"results": [], "error": "搜索被拦截：限流"}})
    outcome = await c.search("q", 8, "t1")
    assert outcome.hits == [] and outcome.error == "搜索被拦截：限流"
    down, _ = client(tmp_path, {"/api/v1/search/query": DependencyUnavailableError("down")})
    assert (await down.search("q", 8, "t1")).error == "搜索服务不可用"


@pytest.mark.unit
async def test_fetch_posts_only_the_chosen_pages_and_maps_their_statuses(tmp_path: Path) -> None:
    reply = {
        "results": [
            {"url": "https://a.example/x", "title": "A", "preview": "正文", "status": "OK"},
            {"url": "https://b.example/y", "title": "B", "snippet": "摘要", "status": "BLOCKED"},
        ]
    }
    c, sidecar = client(tmp_path, {"/api/v1/search/fetch": reply})
    hits = [
        Evidence("https://a.example/x", "A", "sa", "SNIPPET"),
        Evidence("https://b.example/y", "B", "sb", "SNIPPET"),
    ]
    out = await c.fetch(hits, "t1")
    path, body = sidecar.posts[0]
    assert path == "/api/v1/search/fetch" and [i["url"] for i in body["items"]] == [
        h.url for h in hits
    ]
    assert [(e.status, e.text) for e in out] == [("OK", "正文"), ("SNIPPET", "摘要")]


@pytest.mark.unit
async def test_fetch_batches_to_the_sidecar_limit_and_survives_an_outage(tmp_path: Path) -> None:
    hits = [Evidence(f"https://a.example/{i}", "T", "", "SNIPPET") for i in range(12)]
    c, sidecar = client(tmp_path, {"/api/v1/search/fetch": {"results": []}})
    await c.fetch(hits, "t1")
    assert [len(b["items"]) for _, b in sidecar.posts] == [10, 2]
    down, _ = client(tmp_path, {"/api/v1/search/fetch": DependencyUnavailableError("down")})
    out = await down.fetch(hits[:2], "t1")
    assert [e.status for e in out] == ["ERROR", "ERROR"]
