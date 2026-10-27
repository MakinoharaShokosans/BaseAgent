"""两个 sidecar 共用的接入防护：只接受回环 Host，且拒绝带 ``Origin`` 的写请求。

浏览器里的恶意页面向 127.0.0.1 发请求时一定带 ``Origin``（跨站）或使用重绑定的 Host；
Agent 自己用 httpx 调用，两者都不带。sidecar 不导入运行时的任何东西，所以这里独立实现。
"""

from __future__ import annotations

from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LOOPBACK: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_WRITES: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _host(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class LoopbackGuard:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if _host(headers.get("host", "")) not in _LOOPBACK:
            await _refuse(send, "HOST_NOT_ALLOWED")
        elif scope["method"] in _WRITES and "origin" in headers:
            await _refuse(send, "ORIGIN_NOT_ALLOWED")
        else:
            await self._app(scope, receive, send)


async def _refuse(send: Send, code: str) -> None:
    body = f'{{"detail":{{"code":"{code}"}}}}'.encode()
    start: Message = {
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})
