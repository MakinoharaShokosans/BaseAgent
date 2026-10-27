"""目标地址校验与安全抓取。设计文档：docs/04_tools/06_web_search_service.md §4.1、§5。

抓取层是研究隔离区里直接接触外部网络的一环：每一次请求和每一次重定向都要过地址校验，
并且**用校验过的 IP 去连接**（SNI 与 Host 仍是原主机名），防止 DNS 重绑定。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

Resolver = Callable[[str, int], Awaitable[list[str]]]
_WAF: Final = frozenset({403, 429, 503})
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})


class AddressRejected(ValueError):
    """目标地址不被允许（协议、端口、用户信息、内网地址）。"""


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def ip_is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 之类的映射写法按 IPv4 判断
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


@dataclass(frozen=True, slots=True)
class Target:
    url: str
    host: str
    port: int
    ip: str


async def check_url(
    url: str,
    *,
    allowed_ports: Collection[int],
    resolver: Resolver,
    block_private: bool = True,
) -> Target:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise AddressRejected("只允许 http 与 https")
    if parts.username or parts.password or "@" in parts.netloc:
        raise AddressRejected("不允许带用户信息的 URL")
    host = parts.hostname
    if not host:
        raise AddressRejected("缺少主机名")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise AddressRejected("端口不合法") from exc
    if port not in allowed_ports:
        raise AddressRejected(f"端口 {port} 不在允许范围内")
    try:
        # 十进制、十六进制、八进制写法的 IP 字面量交给标准库规范化后再判断
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await resolver(host, port)
        except OSError as exc:
            raise AddressRejected("主机名无法解析") from exc
    if not addresses:
        raise AddressRejected("主机名没有解析结果")
    if block_private and not all(ip_is_public(a) for a in addresses):
        raise AddressRejected("目标地址不被允许")  # 任何一个解析结果是内网就整体拒绝
    return Target(url, host, port, addresses[0])


@dataclass(frozen=True, slots=True)
class Fetched:
    status: str  # OK | BLOCKED | ERROR
    final_url: str = ""
    content_type: str = ""
    body: bytes = b""
    truncated: bool = False
    error: str = ""


class SafeFetcher:
    def __init__(
        self,
        *,
        allowed_ports: Collection[int],
        max_redirects: int,
        max_bytes: int,
        allowed_types: Collection[str],
        user_agent: str,
        timeout_sec: float,
        resolver: Resolver = system_resolver,
        block_private: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._ports = set(allowed_ports)
        self._redirects = max_redirects
        self._max = max_bytes
        self._types = {t.lower() for t in allowed_types}
        self._resolver = resolver
        self._block = block_private
        self._headers = {"User-Agent": user_agent, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"}
        self._client = httpx.AsyncClient(
            timeout=timeout_sec, transport=transport, follow_redirects=False, trust_env=False
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> Fetched:
        current = url
        for _ in range(self._redirects + 1):
            try:
                target = await check_url(
                    current,
                    allowed_ports=self._ports,
                    resolver=self._resolver,
                    block_private=self._block,
                )
            except AddressRejected as exc:
                return Fetched("ERROR", current, error=f"目标地址不被允许：{exc}")
            try:
                async with self._client.stream(
                    "GET",
                    _pin(target),
                    headers={**self._headers, "Host": _host_header(target)},
                    extensions={"sni_hostname": target.host},
                ) as response:
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("location")
                        if not location:
                            return Fetched("ERROR", current, error="重定向缺少 Location")
                        current = urljoin(current, location)
                        continue  # 下一跳重新做地址校验
                    if response.status_code in _WAF:
                        return Fetched("BLOCKED", current, error=f"HTTP {response.status_code}")
                    if response.status_code >= 400:
                        return Fetched("ERROR", current, error=f"HTTP {response.status_code}")
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if kind and kind not in self._types:
                        return Fetched("ERROR", current, kind, error=f"不处理的内容类型 {kind}")
                    body, truncated = await self._read(response)
                    return Fetched("OK", current, kind, body, truncated)
            except httpx.HTTPError as exc:
                return Fetched("ERROR", current, error=f"{type(exc).__name__}")
        return Fetched("ERROR", current, error=f"重定向超过 {self._redirects} 跳")

    async def _read(self, response: httpx.Response) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self._max:
                chunks.append(chunk[: len(chunk) - (size - self._max)])
                return b"".join(chunks), True
            chunks.append(chunk)
        return b"".join(chunks), False


def _host_header(target: Target) -> str:
    default = 443 if target.url.startswith("https") else 80
    return target.host if target.port == default else f"{target.host}:{target.port}"


def _pin(target: Target) -> str:
    """把 URL 的主机换成校验过的 IP：连接用这个地址，不再二次解析。"""
    parts = urlsplit(target.url)
    ip = f"[{target.ip}]" if ":" in target.ip else target.ip
    netloc = ip if target.port in {80, 443} and not parts.port else f"{ip}:{target.port}"
    return parts._replace(netloc=netloc).geturl()
