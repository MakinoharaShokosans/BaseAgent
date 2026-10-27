"""web_search sidecar 的 HTTP 应用。设计文档：docs/04_tools/06_web_search_service.md。

页面正文是不可信数据：这里不做任何信任判断，只负责安全地取、清洗、限量、落盘。
信任隔离由研究隔离区负责。任何失败都以状态表达，不抛业务异常。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from baseagent_config.models import WebSearchConfig
from services.common import LoopbackGuard
from services.web_search.extract import extract_text, title_of
from services.web_search.netguard import SafeFetcher
from services.web_search.providers import SearchHit, SearchProvider

PREFIX = "/api/v1"
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    max_results: int | None = Field(default=None, ge=1)
    fetch: bool = True
    task_id: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True, slots=True)
class WebServices:
    config: WebSearchConfig
    provider: SearchProvider
    fetcher: SafeFetcher
    artifacts_dir: Path
    count_tokens: Any = len


def normalise_url(url: str) -> str:
    """URL 指纹：小写主机、去片段、去默认端口与尾部斜杠，同一页面的不同写法归一。"""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and (
        (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
    ):
        port = None
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def fingerprint(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFKC", text).strip().encode()).hexdigest()[:16]


def create_app(services: WebServices) -> FastAPI:
    cfg = services.config
    app = FastAPI(title="BaseAgent web_search", docs_url=None, redoc_url=None, openapi_url=None)
    host_slots: defaultdict[str, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(cfg.per_host_concurrency)
    )
    global_slots = asyncio.Semaphore(cfg.fetch_concurrency)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        problems = [
            {"loc": [str(p) for p in e["loc"]], "msg": str(e["msg"])} for e in exc.errors()[:10]
        ]
        return JSONResponse(
            {"detail": {"code": "VALIDATION_ERROR", "errors": problems}}, status_code=400
        )

    @app.get(f"{PREFIX}/health")
    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok", "provider": services.provider.name}

    async def fetch_one(hit: SearchHit, task_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "title": hit.title,
            "url": hit.url,
            "snippet": hit.snippet,
            "status": "SNIPPET_ONLY",
            "published_at": None,
            "preview": "",
            "token_estimate": 0,
            "is_truncated": False,
            "artifact_path": None,
            "error": None,
            "fingerprint": fingerprint(normalise_url(hit.url)),
        }
        host = (urlsplit(hit.url).hostname or "").lower()
        async with global_slots, host_slots[host]:
            fetched = await services.fetcher.fetch(hit.url)
        if fetched.status != "OK":
            result.update(status=fetched.status, error=fetched.error)
            return result
        html = fetched.body.decode("utf-8", errors="replace")
        text = (fetched.content_type == "text/plain" and html) or await asyncio.to_thread(
            extract_text, html
        )
        if not text.strip():
            result.update(status="EMPTY", error="页面可达但提取不到正文")
            return result
        if not result["title"]:
            result["title"] = title_of(html)
        tokens = services.count_tokens(text)
        result.update(
            status="OK",
            token_estimate=tokens,
            is_truncated=fetched.truncated,
            fingerprint=fingerprint(text),
        )
        if tokens > cfg.max_content_tokens:
            digest = hashlib.sha256(hit.url.encode()).hexdigest()[:16]
            path = services.artifacts_dir / task_id / f"web_{digest}.md"
            await asyncio.to_thread(_write, path, text)
            result.update(
                preview=text[: cfg.preview_chars], artifact_path=str(path), is_truncated=True
            )
        else:
            result["preview"] = text
        return result

    @app.post(f"{PREFIX}/search/query")
    async def query(body: QueryRequest) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        if not _TASK_ID.match(body.task_id):
            raise HTTPException(
                422, {"code": "INVALID_TASK_ID", "reason": "task_id 只允许 [A-Za-z0-9_-]{1,64}"}
            )
        limit = min(body.max_results or cfg.max_results, cfg.max_results)
        try:
            async with asyncio.timeout(cfg.total_timeout_sec):
                hits = await services.provider.search(body.query.strip(), limit * 2)
                unique: list[SearchHit] = []
                seen: set[str] = set()
                for hit in hits:
                    key = normalise_url(hit.url)
                    if key not in seen:
                        seen.add(key)
                        unique.append(hit)
                chosen = unique[:limit]
                if body.fetch:
                    results = await asyncio.gather(*(fetch_one(h, body.task_id) for h in chosen))
                else:
                    results = [await _snippet_only(h) for h in chosen]
        except TimeoutError as exc:
            raise HTTPException(
                504, {"code": "TOTAL_TIMEOUT", "reason": "检索加抓取超过总超时"}
            ) from exc
        except Exception as exc:  # 搜索提供者挂了：以状态表达，不抛业务异常
            return {"query": body.query, "results": [], "error": f"搜索失败：{type(exc).__name__}"}
        merged = _merge_same_content(list(results))
        return {"query": body.query, "results": merged, "error": None}

    app.add_middleware(LoopbackGuard)
    return app


async def _snippet_only(hit: SearchHit) -> dict[str, Any]:
    return {
        "title": hit.title,
        "url": hit.url,
        "snippet": hit.snippet,
        "status": "SNIPPET_ONLY",
        "published_at": None,
        "preview": "",
        "token_estimate": 0,
        "is_truncated": False,
        "artifact_path": None,
        "error": None,
        "fingerprint": fingerprint(normalise_url(hit.url)),
    }


def _merge_same_content(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """内容相同的页面（镜像、重复收录）只留第一个。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in results:
        key = item["fingerprint"]
        if item["status"] == "OK" and key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
