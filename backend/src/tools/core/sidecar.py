"""sidecar HTTP 客户端。设计文档：04_tools/01_tool_system.md §9。

一个延迟创建、共享连接池的 ``httpx.AsyncClient``；所有失败统一成 ``DependencyUnavailableError``。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from tools.core.errors import DependencyUnavailableError

_OK: Final = (200,)


@dataclass(frozen=True, slots=True)
class SidecarReply:
    status: int
    body: Mapping[str, Any]


class SidecarClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def opened(self) -> bool:
        return self._client is not None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        accepted: Collection[int] = _OK,
        timeout_sec: float | None = None,
    ) -> SidecarReply:
        return await self._send("POST", path, payload, accepted, timeout_sec)

    async def get_json(
        self, path: str, *, accepted: Collection[int] = _OK, timeout_sec: float | None = None
    ) -> SidecarReply:
        return await self._send("GET", path, None, accepted, timeout_sec)

    async def _send(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        accepted: Collection[int],
        timeout_sec: float | None,
    ) -> SidecarReply:
        timeout = (
            httpx.Timeout(timeout_sec) if timeout_sec is not None else httpx.USE_CLIENT_DEFAULT
        )
        try:
            response = await self.http.request(method, path, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise DependencyUnavailableError(
                f"sidecar {self._base_url}{path} 不可达：{exc!r}"
            ) from exc
        if response.status_code not in accepted:
            raise DependencyUnavailableError(
                f"sidecar {self._base_url}{path} 返回了非预期状态码 {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DependencyUnavailableError(f"sidecar {path} 的响应不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise DependencyUnavailableError(f"sidecar {path} 的响应不是 JSON 对象")
        return SidecarReply(response.status_code, body)  # type: ignore[arg-type]
