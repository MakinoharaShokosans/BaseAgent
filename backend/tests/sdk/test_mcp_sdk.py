"""技术验证项 4：mcp 2.x 的会话生命周期是否符合 docs/04_tools/04_mcp.md 的假设。

用 tests/sdk/mcp_probe_server.py 起真实的 stdio 子进程，不联网。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

SERVER = Path(__file__).with_name("mcp_probe_server.py")

pytestmark = pytest.mark.integration


def _params(**kwargs: object) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=[str(SERVER)], **kwargs)  # type: ignore[arg-type]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def _server_pid(session: ClientSession) -> int:
    result = await session.call_tool("pid", {})
    return int(result.content[0].text)  # type: ignore[union-attr]


async def _open(stack: contextlib.AsyncExitStack, params: StdioServerParameters) -> ClientSession:
    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session


# ------------------------------------------------------------------------------
# 基本能力
# ------------------------------------------------------------------------------


async def test_list_and_call_tools() -> None:
    async with contextlib.AsyncExitStack() as stack:
        session = await _open(stack, _params())
        names = {t.name for t in (await session.list_tools()).tools}
        assert {"pid", "add", "getenv", "sleep"} <= names
        result = await session.call_tool("add", {"a": 1, "b": 2})
        assert result.is_error is False
        assert result.content[0].text == "3"  # type: ignore[union-attr]


async def test_tool_input_schema_is_exposed_for_sanitizing() -> None:
    async with contextlib.AsyncExitStack() as stack:
        session = await _open(stack, _params())
        tools = {t.name: t for t in (await session.list_tools()).tools}
        schema = tools["add"].input_schema
        assert schema["properties"]["a"]["type"] == "integer"
        assert tools["add"].description == "加法"


async def test_unknown_tool_is_reported_as_an_error_result_or_exception() -> None:
    async with contextlib.AsyncExitStack() as stack:
        session = await _open(stack, _params())
        try:
            result = await session.call_tool("nope", {})
        except MCPError:
            return
        assert result.is_error is True


# ------------------------------------------------------------------------------
# 子进程环境：不继承父进程的密钥
# ------------------------------------------------------------------------------


@pytest.mark.security
async def test_child_does_not_inherit_parent_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    async with contextlib.AsyncExitStack() as stack:
        session = await _open(stack, _params())
        leaked = await session.call_tool("getenv", {"name": "DEEPSEEK_API_KEY"})
        assert leaked.content[0].text == "<unset>"  # type: ignore[union-attr]


async def test_explicit_env_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    async with contextlib.AsyncExitStack() as stack:
        session = await _open(stack, _params(env={"PROBE_VALUE": "42"}))
        got = await session.call_tool("getenv", {"name": "PROBE_VALUE"})
        assert got.content[0].text == "42"  # type: ignore[union-attr]


# ------------------------------------------------------------------------------
# 生命周期：同一任务内进出
# ------------------------------------------------------------------------------


async def test_same_task_close_leaves_no_child() -> None:
    async with contextlib.AsyncExitStack() as stack:
        session = await _open(stack, _params())
        pid = await _server_pid(session)
        assert _alive(pid)
    assert await _gone(pid)


# ------------------------------------------------------------------------------
# 文档第 1 项风险：在不同任务里建立与释放
# ------------------------------------------------------------------------------


async def test_closing_in_a_different_task_raises() -> None:
    """文档假设成立：建立与释放不在同一任务时，anyio 取消作用域报错。"""
    stack = contextlib.AsyncExitStack()
    session = await asyncio.create_task(_open(stack, _params()))
    pid = await _server_pid(session)
    with pytest.raises(RuntimeError, match="different task"):
        await asyncio.create_task(stack.aclose())
    # 报错之后子进程最终仍被回收，但不能依赖这一点
    assert await _gone(pid)


# ------------------------------------------------------------------------------
# 文档的缓解方案：专用后台任务拥有全部会话
# ------------------------------------------------------------------------------


class SessionOwner:
    """专用后台任务：连接的建立、调用、关闭都在这一个任务里，其他任务通过队列提交。"""

    def __init__(self, params: StdioServerParameters) -> None:
        self._params = params
        self._inbox: asyncio.Queue[tuple[str, dict[str, object], asyncio.Future[object]] | None] = (
            asyncio.Queue()
        )
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                session = await _open(stack, self._params)
                self._ready.set_result(None)
                while (item := await self._inbox.get()) is not None:
                    name, args, future = item
                    try:
                        future.set_result(await session.call_tool(name, args))
                    except Exception as exc:  # 单次调用失败不拆掉连接
                        future.set_exception(exc)
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    async def ready(self) -> None:
        await self._ready

    async def call_raw(self, name: str, args: dict[str, object]) -> Any:
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        await self._inbox.put((name, args, future))
        return await future

    async def call(self, name: str, args: dict[str, object]) -> str:
        result = await self.call_raw(name, args)
        return result.content[0].text  # type: ignore[no-any-return]

    async def close(self) -> None:
        await self._inbox.put(None)
        await self._task


async def test_owner_task_pattern_serves_calls_from_other_tasks_and_closes_cleanly() -> None:
    owner = SessionOwner(_params())
    await owner.ready()
    pid = int(await owner.call("pid", {}))
    # 多个不同任务并发调用
    results = await asyncio.gather(
        *(asyncio.create_task(owner.call("add", {"a": i, "b": i})) for i in range(5))
    )
    assert results == ["0", "2", "4", "6", "8"]
    # 由另一个任务触发关闭：不报作用域错误
    await asyncio.create_task(owner.close())
    assert await _gone(pid)


async def test_owner_survives_a_failing_call() -> None:
    owner = SessionOwner(_params())
    await owner.ready()
    # 参数校验失败不抛异常，而是返回 is_error=True 的结果：工具层要按结果判断，不能只靠 try/except
    bad = await owner.call_raw("add", {"a": "not-a-number", "b": 1})
    assert bad.is_error is True
    assert await owner.call("add", {"a": 1, "b": 1}) == "2"
    await owner.close()


async def test_cancelling_the_owner_task_kills_the_child() -> None:
    owner = SessionOwner(_params())
    await owner.ready()
    pid = int(await owner.call("pid", {}))
    owner._task.cancel()  # 模拟应用关闭时强行取消
    with contextlib.suppress(asyncio.CancelledError):
        await owner._task
    assert await _gone(pid)


# ------------------------------------------------------------------------------
# 调用超时
# ------------------------------------------------------------------------------


async def test_call_timeout_raises_and_the_session_stays_usable() -> None:
    async with contextlib.AsyncExitStack() as stack:
        session = await _open(stack, _params())
        # 2.x 里超时参数是 float 秒，不是 timedelta
        with pytest.raises(MCPError) as info:
            await session.call_tool("sleep", {"seconds": 3}, read_timeout_seconds=0.5)
        assert "timed out" in str(info.value).lower()
        # 超时后会话仍可用（服务器端那次调用仍在跑，随后到达的结果被丢弃）
        again = await session.call_tool("add", {"a": 2, "b": 2}, read_timeout_seconds=10)
        assert again.content[0].text == "4"  # type: ignore[union-attr]
