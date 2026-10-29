"""MCP 管理器：懒连接、专用后台任务拥有全部会话、故障隔离、带退避的重试。

设计文档：docs/04_tools/04_mcp.md §4、§9。

anyio 的取消作用域与创建它的任务绑定，连接必须在同一个任务里建立和释放。所以建立、刷新之外的
握手与关闭都由一个专用的后台任务（owner）执行，其他任务通过队列提交；调用本身（``call_tool``）
不进入任何上下文，可以在任意任务里发起。
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from loguru import logger
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from agent_runtime.core import ToolResult
from baseagent_config.models import McpConfig, McpServerConfig
from mcps.naming import assign_names
from mcps.vetting import Rejection, Vetted, vet_tool

Status = Literal["disabled", "idle", "connected", "failed"]

_ERROR_CLIP: Final = 200
_LAUNCHERS: Final = frozenset({"npx", "bunx", "uvx", "pnpx"})
_PINNED: Final = re.compile(r".+@[^@/]+$")
_NON_TEXT: Final = {"image": "<image content>", "audio": "<audio content>"}


def unpinned_package(cfg: McpServerConfig) -> str | None:
    """``npx -y 包名`` 这类启动方式每次可能拉到不同的代码；返回未固定版本的包名，否则 None。"""
    if cfg.transport != "stdio" or os.path.basename(cfg.command) not in _LAUNCHERS:
        return None
    package = next((a for a in cfg.args if not a.startswith("-")), None)
    if package is None or package.startswith((".", "/")):
        return None
    return None if _PINNED.match(package[1:] if package.startswith("@") else package) else package


def resolve_env(
    env: Mapping[str, str], environ: Mapping[str, str], *, server: str
) -> dict[str, str]:
    """``env:VAR`` 引用换成真实值；未设置时告警并传空串，不阻断启动。值永远不进日志。"""
    out: dict[str, str] = {}
    for key, value in env.items():
        if value.startswith("env:"):
            ref = value[4:]
            if ref not in environ:
                logger.warning("MCP 服务器 {} 的环境变量 {} 引用的 {} 未设置", server, key, ref)
            out[key] = environ.get(ref, "")
        else:
            out[key] = value
    return out


def flatten(blocks: list[Any]) -> str:
    """内容块压平为文本；非文本块只留类型提示，信息会丢失（设计文档 §7）。"""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        else:
            kind = getattr(block, "type", "unknown")
            parts.append(_NON_TEXT.get(kind, f"<{kind} content>"))
    return "\n".join(parts)


@dataclass(slots=True)
class ServerState:
    name: str
    config: McpServerConfig
    status: Status = "idle"
    session: ClientSession | None = None
    tools: list[Vetted] = field(default_factory=list[Vetted])
    #: 最近一次成功列出的、通过消毒的工具。连接断开不清空：索引不反映瞬时可用性（决策 0007）
    known: list[Vetted] = field(default_factory=list[Vetted])
    rejected: list[Rejection] = field(default_factory=list[Rejection])
    last_error: str = ""
    failures: int = 0
    retry_after: float = 0.0


@dataclass(slots=True)
class _Cmd:
    kind: Literal["connect", "drop", "stop"]
    server: str
    future: asyncio.Future[Any]
    session: ClientSession | None = None


class McpManager:
    def __init__(
        self,
        config: McpConfig,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        python: str = sys.executable,
    ) -> None:
        self._cfg = config
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._python = python
        self._servers: dict[str, ServerState] = {
            name: ServerState(name, sc, "idle" if sc.enabled else "disabled")
            for name, sc in config.servers.items()
        }
        self._locks = {name: asyncio.Lock() for name in self._servers}
        self._idempotent: set[str] = set()
        #: 界面覆盖层：None 表示沿用配置
        self._global_enabled: bool | None = None
        self._overrides: dict[str, dict[str, Any]] = {}
        self._queue: asyncio.Queue[_Cmd] | None = None
        self._owner: asyncio.Task[None] | None = None
        self._closed = False
        for name, sc in config.servers.items():
            package = unpinned_package(sc) if sc.enabled else None
            if package:
                logger.warning(
                    "MCP 服务器 {} 的包 {} 没有固定版本，每次启动可能拉到不同的代码", name, package
                )

    # ------------------------------------------------------------------ 对任务的接口

    @property
    def active(self) -> bool:
        return self._global_on() and any(self._enabled(s) for s in self._servers.values())

    @property
    def global_enabled(self) -> bool:
        return self._global_on()

    def _global_on(self) -> bool:
        return self._cfg.enabled if self._global_enabled is None else self._global_enabled

    def _enabled(self, state: ServerState) -> bool:
        override = self._overrides.get(state.name, {}).get("enabled")
        local = state.config.enabled if override is None else bool(override)
        return self._global_on() and local

    def _exposure(self, state: ServerState) -> str:
        override = self._overrides.get(state.name, {}).get("exposure")
        if override in ("pinned", "deferred"):
            return str(override)
        return state.config.exposure or self._cfg.exposure

    def _disabled_tools(self, state: ServerState) -> list[str]:
        raw = self._overrides.get(state.name, {}).get("disabled_tools")
        return [str(t) for t in raw] if isinstance(raw, list) else []

    def apply_policy(
        self,
        *,
        enabled: bool | None,
        servers: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """应用界面覆盖层；运行中的连接保留，但不再暴露为工具，下次任务才按新状态装配。"""
        self._global_enabled = enabled
        self._overrides = {name: dict(values) for name, values in servers.items()}
        for state in self._servers.values():
            if not self._enabled(state):
                state.status = "disabled"
                state.last_error = ""
            elif state.status == "disabled":
                state.status = "idle"

    def server_status(self, name: str) -> dict[str, Any] | None:
        state = self._servers.get(name)
        if state is None:
            return None
        return self._status_of(state)

    async def reconnect(self, name: str) -> dict[str, Any] | None:
        state = self._servers.get(name)
        if state is None:
            return None
        if state.session is not None:
            await self._drop(state, state.session, "手动重连")
        state.failures, state.retry_after, state.last_error = 0, 0.0, ""
        if self._enabled(state):
            await self._ensure(name)
        return self._status_of(state)

    def is_idempotent(self, exposed_name: str) -> bool:
        return exposed_name in self._idempotent

    def exposure_of(self, server: str) -> str:
        """服务器的暴露方式：界面覆盖 > 自己的配置 > 全局默认。"""
        state = self._servers.get(server)
        return self._exposure(state) if state else self._cfg.exposure

    async def tools(self) -> list[Any]:
        """工具适配器。任务准备阶段调用：连接缺失的服务器、重新列出并重新消毒；
        用的是各服务器最近一次成功的消毒列表，所以服务器暂时不可用不改变工具集。"""
        from mcps.adapter import McpTool

        if not self.active or self._closed:
            return []
        await asyncio.gather(
            *(self._ensure(name) for name, state in self._servers.items() if self._enabled(state))
        )
        pairs = [
            (state.name, tool.name)
            for state in self._servers.values()
            if self._enabled(state)
            for tool in state.known
            if tool.name not in self._disabled_tools(state)
        ]
        names = assign_names(pairs, self._cfg.exposed_name_max_len)
        out: list[McpTool] = []
        self._idempotent = set()
        for state in self._servers.values():
            if not self._enabled(state):
                continue
            disabled = self._disabled_tools(state)
            for tool in state.known:  # 暂时断开的服务器仍在：调用时返回失败，索引保持稳定
                if tool.name in disabled:
                    continue
                exposed = names[(state.name, tool.name)]
                idem = any(fnmatch.fnmatchcase(tool.name, p) for p in state.config.idempotent_tools)
                if idem:
                    self._idempotent.add(exposed)
                out.append(
                    McpTool(
                        self,
                        server=state.name,
                        tool=tool.name,
                        exposed=exposed,
                        description=tool.description,
                        parameters=tool.schema,
                        idempotent=idem,
                        timeout_sec=float(self._cfg.call_timeout_sec) + 5.0,
                    )
                )
        return out

    async def call(self, server: str, tool: str, args: Mapping[str, Any]) -> ToolResult:
        state = self._servers.get(server)
        session = state.session if state else None
        if state is None or session is None:
            why = f"：{state.last_error}" if state and state.last_error else ""
            return ToolResult.failure(f"MCP 服务器 {server} 当前不可用{why}")
        limit = float(self._cfg.call_timeout_sec)
        started = time.monotonic()
        try:
            async with asyncio.timeout(limit):
                raw = await session.call_tool(tool, dict(args), read_timeout_seconds=limit)
        except MCPError as exc:
            if (
                exc.code != types.CONNECTION_CLOSED
            ):  # 服务器明确报错（未知工具、参数不合法）：连接仍可用
                return ToolResult.failure(f"MCP 调用失败：{_clip(str(exc))}")
            await self._drop(state, session, "连接已关闭")
            return ToolResult.failure("MCP 服务器中途退出，已断开该连接")
        except Exception as exc:  # 超时与传输故障：连接视为失效，下一次重新握手
            await self._drop(state, session, f"调用异常：{type(exc).__name__}")
            if isinstance(exc, TimeoutError):
                return ToolResult.failure(f"MCP 调用超时（{limit:g} 秒），已断开该连接")
            return ToolResult.failure(f"MCP 调用失败：{type(exc).__name__}: {_clip(str(exc))}")
        elapsed = int((time.monotonic() - started) * 1000)
        if not isinstance(raw, types.CallToolResult):
            return ToolResult.failure("MCP 服务器返回了不受支持的结果类型")
        text = flatten(list(raw.content))
        if raw.is_error:
            return ToolResult.failure(text or "MCP 工具报告错误", duration_ms=elapsed)
        return ToolResult(ok=True, content=text, duration_ms=elapsed)

    def status(self) -> list[dict[str, Any]]:
        """自省：每个服务器的状态、工具数、被拒工具与原因、最近错误。不含环境变量值。"""
        return [self._status_of(state) for state in self._servers.values()]

    def _status_of(self, state: ServerState) -> dict[str, Any]:
        now = self._clock()
        disabled_tools = self._disabled_tools(state)
        preview = self._command_preview(state)
        visible = [t.name for t in state.tools if t.name not in disabled_tools]
        return {
            "name": state.name,
            "transport": state.config.transport,
            "status": state.status,
            "enabled": self._enabled(state),
            "exposure": self._exposure(state),
            "disabled_tools": disabled_tools,
            "command_preview": preview,
            "tool_count": len(visible),
            "tools": visible,
            "rejected": [{"tool": r.tool, "reason": r.reason} for r in state.rejected],
            "last_error": state.last_error,
            "retry_in_sec": max(0, round(state.retry_after - now))
            if state.status == "failed"
            else 0,
        }

    def _command_preview(self, state: ServerState) -> str:
        cfg = state.config
        if cfg.transport == "sse":
            return cfg.url
        parts = [cfg.command, *cfg.args]
        return " ".join(part for part in parts if part)

    async def aclose(self) -> None:
        self._closed = True
        owner, queue = self._owner, self._queue
        if owner is None or queue is None:
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        queue.put_nowait(_Cmd("stop", "", future))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(future, timeout=30)
        await owner
        for state in self._servers.values():
            state.session = None
            if state.status == "connected":
                state.status = "idle"
        self._owner = self._queue = None

    # ------------------------------------------------------------------ 连接

    async def _ensure(self, name: str) -> None:
        state = self._servers[name]
        if not self._enabled(state):
            return
        async with self._locks[name]:
            # 刷新失败：连接已被丢弃，就在本任务里重新握手一次，不让它白白错过一个任务
            if state.session is not None and (
                not self._cfg.refresh_tools_each_task or await self._refresh(state)
            ):
                return
            if self._clock() < state.retry_after:
                return  # 退避中：本任务不带这个服务器，不阻塞
            try:
                session = await self._submit("connect", name)
                self._adopt(state, session, await self._list(session))
            except Exception as exc:
                self._fail(state, exc)

    def _adopt(self, state: ServerState, session: ClientSession, listed: list[Any]) -> None:
        cfg = self._cfg
        allowed = set(state.config.allow_tools)
        tools: list[Vetted] = []
        rejected: list[Rejection] = []
        for item in listed:
            if allowed and item.name not in allowed:
                continue  # 未在 allow_tools 里：不暴露，也不算被拒
            verdict = vet_tool(
                item.name,
                item.description,
                item.input_schema,
                max_description_chars=cfg.max_description_chars,
                reject_on_injection=cfg.reject_on_injection,
            )
            if isinstance(verdict, Rejection):
                logger.warning(
                    "MCP {} 的工具被拒绝：{}（{}）", state.name, verdict.tool, verdict.reason
                )
                rejected.append(verdict)
            else:
                tools.append(verdict)
        state.session, state.tools, state.rejected = session, tools, rejected
        state.known = list(tools)
        state.status, state.last_error, state.failures, state.retry_after = "connected", "", 0, 0.0

    async def _list(self, session: ClientSession) -> list[Any]:
        async with asyncio.timeout(float(self._cfg.connection_timeout_sec)):
            return list((await session.list_tools()).tools)

    async def _refresh(self, state: ServerState) -> bool:
        """每个任务重新 tools/list 并重新消毒：服务器可能增减工具或改了描述。"""
        session = state.session
        assert session is not None
        try:
            self._adopt(state, session, await self._list(session))
        except Exception as exc:
            await self._drop(state, session, f"刷新工具列表失败：{type(exc).__name__}")
            return False
        return True

    def _fail(self, state: ServerState, exc: BaseException) -> None:
        backoff = self._cfg.retry_backoff_sec
        state.failures += 1
        wait = backoff[min(state.failures - 1, len(backoff) - 1)] if backoff else 0
        state.status, state.session = "failed", None
        state.tools, state.rejected = [], []
        state.retry_after = self._clock() + wait
        state.last_error = f"{type(exc).__name__}: {_clip(str(exc))}"
        logger.warning(
            "MCP 服务器 {} 连接失败（第 {} 次，{} 秒后重试）：{}",
            state.name,
            state.failures,
            wait,
            state.last_error,
        )

    async def _drop(self, state: ServerState, session: ClientSession, reason: str) -> None:
        if state.session is not session:
            return  # 已被别的调用换掉了
        state.session, state.status = None, "idle"
        state.tools, state.rejected = [], []
        state.last_error = reason
        with contextlib.suppress(Exception):
            await self._submit("drop", state.name, session)

    # ------------------------------------------------------------------ 专用任务

    async def _submit(
        self, kind: Literal["connect", "drop"], server: str, session: ClientSession | None = None
    ) -> Any:
        if self._queue is None or self._owner is None or self._owner.done():
            self._queue = asyncio.Queue()
            self._owner = asyncio.create_task(self._run(self._queue), name="mcp-owner")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Cmd(kind, server, future, session))
        return await future

    async def _run(self, queue: asyncio.Queue[_Cmd]) -> None:
        stacks: dict[str, contextlib.AsyncExitStack] = {}
        sessions: dict[str, ClientSession] = {}
        try:
            while True:
                cmd = await queue.get()
                if cmd.kind == "stop":
                    await self._close_all(stacks)
                    cmd.future.set_result(None)
                    return
                try:
                    if cmd.kind == "connect":
                        await self._close(stacks, cmd.server)
                        sessions[cmd.server] = await self._open(stacks, cmd.server)
                        result: Any = sessions[cmd.server]
                    else:
                        if sessions.get(cmd.server) is cmd.session:
                            await self._close(stacks, cmd.server)
                            sessions.pop(cmd.server, None)
                        result = None
                    if not cmd.future.done():
                        cmd.future.set_result(result)
                except Exception as exc:
                    if not cmd.future.done():
                        cmd.future.set_exception(exc)
        finally:
            await self._close_all(stacks)

    async def _open(self, stacks: dict[str, contextlib.AsyncExitStack], name: str) -> ClientSession:
        cfg = self._servers[name].config
        stack = contextlib.AsyncExitStack()
        try:
            async with asyncio.timeout(float(self._cfg.connection_timeout_sec)):
                if cfg.transport == "sse":
                    streams = await stack.enter_async_context(sse_client(cfg.url))
                else:
                    streams = await stack.enter_async_context(stdio_client(self._stdio(name, cfg)))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
        except BaseException:
            with contextlib.suppress(Exception):
                await stack.aclose()  # 握手阶段任何失败都关掉已进入的上下文，不留半开的子进程
            raise
        stacks[name] = stack
        return session

    def _stdio(self, name: str, cfg: McpServerConfig) -> StdioServerParameters:
        env = resolve_env(cfg.env, self._environ, server=name)
        if os.name != "posix":
            return StdioServerParameters(command=cfg.command, args=list(cfg.args), env=env)
        c = self._cfg
        limits = (
            cfg.rlimit_as_mb or c.rlimit_as_mb,
            cfg.rlimit_fsize_mb or c.rlimit_fsize_mb,
            cfg.rlimit_cpu_sec or c.rlimit_cpu_sec,
        )
        wrapper = ["-m", "mcps.wrapper", *(str(v) for v in limits), "--", cfg.command, *cfg.args]
        return StdioServerParameters(command=self._python, args=wrapper, env=env)

    @staticmethod
    async def _close(stacks: dict[str, contextlib.AsyncExitStack], name: str) -> None:
        stack = stacks.pop(name, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as exc:
                logger.warning("关闭 MCP 服务器 {} 时出错：{!r}", name, exc)

    async def _close_all(self, stacks: dict[str, contextlib.AsyncExitStack]) -> None:
        for name in list(stacks):
            await self._close(stacks, name)


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _ERROR_CLIP else flat[: _ERROR_CLIP - 1] + "…"
