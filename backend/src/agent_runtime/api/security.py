"""接入闸门：纯 ASGI 中间件，不碰请求体内容（对 SSE 透明）。设计文档：docs/09_api/03 §2-5。

Host 校验（防 DNS 重绑定）、Origin 校验（防跨站写）、令牌认证、失败限速、请求体大小上限、
``X-Trace-ID`` 的沿用与生成。失败响应与路由用同一套错误形状。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Collection, MutableMapping
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_runtime.api.errors import TRACE_HEADER, dumps, error_body
from agent_runtime.api.token import FailureLimiter, tokens_match

_WRITE_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_TRACE_OK: Final = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
EXEMPT_PATHS: Final = frozenset({"/", "/health", "/api/v1/health"})
DOC_PATHS: Final = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


def host_name(header: str) -> str:
    """去掉端口，兼容 ``[::1]:8000``。"""
    value = header.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class AccessGate:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None,
        allowed_hosts: Collection[str] = (),
        allowed_origins: Collection[str] = (),
        max_body_bytes: int = 2_000_000,
        large_body_paths: Collection[tuple[str, int]] = (),
        docs_enabled: bool = True,
        limiter: FailureLimiter | None = None,
    ) -> None:
        self._app = app
        self._token = token
        self._hosts = _LOOPBACK_HOSTS | {h.lower() for h in allowed_hosts}
        self._origins = {o.rstrip("/").lower() for o in allowed_origins}
        self._max = max_body_bytes
        self._large = tuple(large_body_paths)
        self._exempt = EXEMPT_PATHS | (DOC_PATHS if docs_enabled else frozenset())
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = _headers(scope)
        trace_id = headers.get("x-trace-id", "")
        if not _TRACE_OK.match(trace_id):
            trace_id = uuid.uuid4().hex
        scope.setdefault("state", {})["trace_id"] = trace_id

        async def traced_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                if not any(k.lower() == TRACE_HEADER.lower().encode() for k, _ in response_headers):
                    response_headers.append((TRACE_HEADER.lower().encode(), trace_id.encode()))
                message = {**message, "headers": response_headers}
            await send(message)

        refusal = self._refuse(scope, headers)
        if refusal is not None:
            status, code, text = refusal
            await _respond(traced_send, status, code, text, trace_id)
            return

        limit = self._body_limit(scope["path"])
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            await _respond(traced_send, 413, "PAYLOAD_TOO_LARGE", "请求体过大", trace_id)
            return

        started = False
        seen = 0
        over = False

        async def counted_receive() -> Message:
            nonlocal seen, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # 框架会把读体失败包装成 400，所以这里让它看到断开，响应由闸门自己给
                    over = True
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if over:
                return  # 超限之后应用产生的任何响应都丢弃
            started = started or message["type"] == "http.response.start"
            await traced_send(message)

        try:
            await self._app(scope, counted_receive, tracking_send)
        except Exception:
            if not over:
                raise
        if over and not started:
            await _respond(traced_send, 413, "PAYLOAD_TOO_LARGE", "请求体过大", trace_id)

    def _refuse(
        self, scope: Scope, headers: MutableMapping[str, str]
    ) -> tuple[int, str, str] | None:
        host = host_name(headers.get("host", ""))
        if host not in self._hosts:
            return 403, "HOST_NOT_ALLOWED", "Host 不在允许范围内"
        method, path = scope["method"], scope["path"]
        origin = headers.get("origin")
        if (
            method in _WRITE_METHODS
            and origin is not None
            and origin.rstrip("/").lower() not in self._origins
        ):
            return 403, "ORIGIN_NOT_ALLOWED", "Origin 不在允许范围内"
        if method == "OPTIONS" or path in self._exempt or self._token is None:
            return None
        source = (scope.get("client") or ("-", 0))[0]
        if self._limiter is not None and self._limiter.blocked(source):
            return 429, "AUTH_RATE_LIMITED", "认证失败次数过多，请稍后再试"
        presented = _bearer(headers.get("authorization", ""))
        if presented is None or not tokens_match(self._token, presented):
            if self._limiter is not None:
                self._limiter.record_failure(source)
            return 401, "UNAUTHORIZED", "未认证"  # 不区分缺少与错误，不回显令牌
        return None

    def _body_limit(self, path: str) -> int:
        for suffix, limit in self._large:
            if path.endswith(suffix):
                return limit
        return self._max


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}


def _bearer(value: str) -> str | None:
    scheme, _, rest = value.partition(" ")
    return rest.strip() if scheme.lower() == "bearer" and rest.strip() else None


async def _respond(
    send: Callable[[Message], Awaitable[None]],
    status: int,
    code: str,
    message: str,
    trace_id: str,
) -> None:
    body = dumps(error_body(code, message, trace_id))
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


__all__ = ["DOC_PATHS", "EXEMPT_PATHS", "AccessGate", "host_name"]
