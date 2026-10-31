"""可插拔的搜索提供者。设计文档：04_tools/06_web_search_service.md §1、§5。

新增提供者必须声明是否需要密钥、是否可能返回内网链接（后者由抓取层的地址校验兜底）。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlsplit

import httpx


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    category: str = "general"
    img_src: str | None = None
    thumbnail_src: str | None = None
    engine: str = ""


class SearchProvider(Protocol):
    name: str
    needs_key: bool
    may_return_private_links: bool

    async def search(
        self, query: str, limit: int, category: str = "general"
    ) -> list[SearchHit]: ...


class SearchBlocked(RuntimeError):
    """搜索源拒绝了请求（反爬、限流）。与“搜到 0 条”不同，必须让调用方看见。"""


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

    async def search(
        self, query: str, limit: int, category: str = "general"
    ) -> list[SearchHit]:
        response = await self._client.post("https://html.duckduckgo.com/html/", data={"q": query})
        response.raise_for_status()
        if response.status_code != 200 or "result__a" not in response.text:
            # 202 是反爬验证页：必须当作失败上报，不能当成“没有结果”
            raise SearchBlocked(f"DuckDuckGo 拒绝了请求（HTTP {response.status_code}）")
        parser = _DuckParser()
        parser.feed(response.text)
        hits = [
            SearchHit(
                re.sub(r"\s+", " ", t).strip(),
                url,
                re.sub(r"\s+", " ", s).strip(),
                category=category,
            )
            for url, t, s in parser.hits
            if url.startswith(("http://", "https://"))
        ]
        return hits[:limit]


class SearxngProvider:
    """SearXNG 元搜索提供者：支持公网或自建实例池，按顺序尝试，自动故障转移。

    免 API Key、免额外安装进程（走公网节点），直接获取标准 JSON 响应，聚合多引擎结果。
    支持 categories 参数搜索图片（images）、IT（it）、新闻（news）、科学（science）等多品类。
    """

    name = "searxng"
    needs_key = False
    may_return_private_links = True

    def __init__(
        self,
        *,
        instances: Sequence[str],
        user_agent: str,
        timeout_sec: float,
        language: str = "zh-CN",
        deadline_sec: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._instances = tuple(i.rstrip("/") for i in instances if i.strip())
        self._language = language.strip()
        self._deadline = deadline_sec if deadline_sec is not None else timeout_sec * 2.0
        self._client = httpx.AsyncClient(
            timeout=timeout_sec,
            transport=transport,
            headers={"User-Agent": user_agent},
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(
        self, query: str, limit: int, category: str = "general"
    ) -> list[SearchHit]:
        if not self._instances:
            raise SearchBlocked("未配置任何 SearXNG 实例")

        started = time.monotonic()
        failures: list[str] = []
        for instance in self._instances:
            if failures and time.monotonic() - started >= self._deadline:
                break
            params: dict[str, Any] = {"q": query, "format": "json"}
            if category and category != "general":
                params["categories"] = category
            if self._language:
                params["language"] = self._language
            try:
                response = await self._client.get(f"{instance}/search", params=params)
                if response.status_code != 200:
                    failures.append(f"{instance}：HTTP {response.status_code}")
                    continue
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                failures.append(f"{instance}：{exc}")
                continue

            results = data.get("results")
            if not isinstance(results, list):
                failures.append(f"{instance}：响应格式非预期（缺少 results 列表）")
                continue

            hits = [
                SearchHit(
                    title=re.sub(r"\s+", " ", str(r.get("title", ""))).strip(),
                    url=str(r.get("url", "")),
                    snippet=re.sub(r"\s+", " ", str(r.get("content", ""))).strip(),
                    category=category,
                    img_src=str(r["img_src"]) if r.get("img_src") else None,
                    thumbnail_src=str(r["thumbnail_src"]) if r.get("thumbnail_src") else None,
                    engine=str(r.get("engine", "")),
                )
                for r in results
                if str(r.get("url", "")).startswith(("http://", "https://"))
            ]
            if hits:
                return hits[:limit]

        if failures and len(failures) == len(self._instances):
            raise SearchBlocked("所有 SearXNG 实例都失败：" + "；".join(failures))
        return []


class DdgsProvider:
    """``ddgs`` 库，按顺序依次试各个后端，取第一个有结果的。

    ``auto`` 每次随机挑后端，实测约三分之一的调用返回空；单个后端也各有 25%–60% 的失败率，
    所以不押某一个：依次尝试，一个返回空或被拦就换下一个。
    """

    name = "ddgs"
    needs_key = False
    may_return_private_links = True

    def __init__(
        self,
        *,
        timeout_sec: float,
        backends: Sequence[str] = ("yahoo", "bing", "google"),
        deadline_sec: float | None = None,
    ) -> None:
        self._timeout = int(timeout_sec)
        self._backends = tuple(backends) or ("auto",)
        # 整条链的总时限：前面的后端慢，后面的就不试了，免得撞上 sidecar 的总超时
        self._deadline = deadline_sec if deadline_sec is not None else timeout_sec * 1.5

    async def search(
        self, query: str, limit: int, category: str = "general"
    ) -> list[SearchHit]:
        return await asyncio.to_thread(self._search, query, limit, category)

    def _search(self, query: str, limit: int, category: str = "general") -> list[SearchHit]:
        from ddgs import DDGS  # 仅此提供者需要，延迟导入
        from ddgs.exceptions import DDGSException

        started = time.monotonic()
        failures: list[str] = []
        for backend in self._backends:
            if failures and time.monotonic() - started >= self._deadline:
                break
            try:
                rows: list[dict[str, Any]] = DDGS(timeout=self._timeout).text(
                    query, max_results=limit, backend=backend
                )
            except DDGSException as exc:
                if "No results found" not in str(exc):
                    failures.append(f"{backend}：{exc}")
                continue
            hits = _rows_to_hits(rows, limit, category=category)
            if hits:
                return hits
        if failures and len(failures) == len(self._backends):
            raise SearchBlocked("所有搜索后端都失败：" + "；".join(failures))
        return []


def _rows_to_hits(
    rows: Sequence[dict[str, Any]], limit: int, category: str = "general"
) -> list[SearchHit]:
    return [
        SearchHit(
            re.sub(r"\s+", " ", str(row.get("title", ""))).strip(),
            str(row.get("href", "")),
            re.sub(r"\s+", " ", str(row.get("body", ""))).strip(),
            category=category,
        )
        for row in rows
        if str(row.get("href", "")).startswith(("http://", "https://"))
    ][:limit]
