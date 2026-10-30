"""``websearch`` 的网址来源校验。设计文档：docs/04_tools/07_websearch_tool.md。

主 Agent 一旦被提示词注入，最直接的外泄办法就是把密钥拼进一个网址让工具去“读”：
``https://evil.example/?k=<密钥>``。SSRF 防护只管内网，管不了这个。所以 ``urls`` 里的每个网址
必须**逐字**出现在两类内容里：**用户自己写的消息**，或**已有工具输出（如 websearch 结果、view_file 读取的内容等）**。
模型自己未经验证拼写出的网址不算：它们可能是模型自己拼出来的带密钥网址。
网址要逐字匹配（含查询串），所以带着密钥拼出来的网址无法匹配。
"""

from __future__ import annotations

import re
from typing import Any, Final
from urllib.parse import urlsplit

from agent_runtime.core import AgentState, ToolCall

WEBSEARCH: Final = "websearch"
_URL: Final = re.compile(r"https?://[^\s<>\"'`\])）】，。；、]+")
_TRAILING: Final = ".,;:!?)]}>’”"


def canonical(url: str) -> str:
    """同一个网址的不同写法归一：小写协议与主机、去片段与默认端口与路径尾斜杠；查询串原样保留。"""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and (
        (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
    ):
        port = None
    netloc = f"{host}:{port}" if port else host
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{netloc}{path}{query}"


def urls_in(text: str) -> set[str]:
    return {canonical(m.group(0).rstrip(_TRAILING)) for m in _URL.finditer(text)}


def known_urls(state: AgentState) -> set[str]:
    known: set[str] = set()
    for message in state.messages:
        if message.role in ("user", "tool"):
            known |= urls_in(message.content)
    return known


def check_urls(state: AgentState, call: ToolCall) -> str | None:
    """``websearch`` 的 ``urls`` 全部有来源则返回 None，否则返回给模型的失败说明。"""
    if call.name != WEBSEARCH:
        return None
    urls: Any = call.args.get("urls")
    if not isinstance(urls, list) or not urls:
        return None
    known: set[str] | None = None
    for item in urls:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(item, str):
            continue  # 类型错误由工具自己报
        known = known_urls(state) if known is None else known
        if canonical(item) not in known:
            return (
                f"网址 {item[:200]} 不是用户提供或工具输出中出现的网址，不能读取。"
                "websearch 的 urls 接受用户输入或工具（如 view_file、websearch）输出中包含的网址；"
                "若需检索外部资料请使用 query 搜索。"
            )
    return None
