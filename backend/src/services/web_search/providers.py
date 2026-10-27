"""可插拔的搜索提供者。设计文档：04_tools/06_web_search_service.md §1、§5。

新增提供者必须声明是否需要密钥、是否可能返回内网链接（后者由抓取层的地址校验兜底）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlsplit

import httpx


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    name: str
    needs_key: bool
    may_return_private_links: bool

    async def search(self, query: str, limit: int) -> list[SearchHit]: ...


class _DuckParser(HTMLParser):
    """解析 DuckDuckGo 的 HTML 结果页：``a.result__a`` 标题与链接，``.result__snippet`` 摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[list[str]] = []
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class") or ""
        href = dict(attrs).get("href") or ""
        if tag == "a" and "result__a" in classes:
            self.hits.append([_target(href), "", ""])
            self._field = "title"
        elif "result__snippet" in classes and self.hits:
            self._field = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"a", "td", "div"}:
            self._field = None

    def handle_data(self, data: str) -> None:
        if not self.hits or self._field is None:
            return
        index = 1 if self._field == "title" else 2
        self.hits[-1][index] += data


def _target(href: str) -> str:
    """DuckDuckGo 把真实地址放在 ``uddg`` 参数里的跳转链接中。"""
    parts = urlsplit(href if "://" in href else "https:" + href if href.startswith("//") else href)
    target = parse_qs(parts.query).get("uddg")
    return unquote(target[0]) if target else href


class DuckDuckGoProvider:
    name = "ddg"
    needs_key = False
    may_return_private_links = True  # 搜索结果是外部输入：抓取前必须过地址校验

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_sec: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            transport=transport,
            headers={"User-Agent": user_agent},
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        response = await self._client.post("https://html.duckduckgo.com/html/", data={"q": query})
        response.raise_for_status()
        parser = _DuckParser()
        parser.feed(response.text)
        hits = [
            SearchHit(re.sub(r"\s+", " ", t).strip(), url, re.sub(r"\s+", " ", s).strip())
            for url, t, s in parser.hits
            if url.startswith(("http://", "https://"))
        ]
        return hits[:limit]
