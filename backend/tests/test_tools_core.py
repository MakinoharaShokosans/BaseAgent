"""工具注册表与派发器。出处：docs/04_tools/01_tool_system.md §3-7、§12。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest

from agent_runtime.core import AegisTool, ToolResult
from tools.core import ToolDispatcher, ToolRegistry, ToolRegistryError


class Fake(AegisTool):
    def __init__(
        self,
        name: str,
        *,
        trust: str = "trusted",
        idempotent: bool = False,
        timeout_sec: float = 5.0,
        result: ToolResult | None = None,
        behaviour: Any = None,
    ) -> None:
        self.name = name
        self.description = f"{name} 工具"
        self.parameters = {"type": "object", "properties": {"x": {"type": "string"}}}
        self.trust = trust  # type: ignore[assignment]
        self.idempotent = idempotent
        self.timeout_sec = timeout_sec
        self._result = result or ToolResult(ok=True, content=f"{name}-ok")
        self._behaviour = behaviour
        self.calls: list[Mapping[str, Any]] = []

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        self.calls.append(args)
        if self._behaviour is not None:
            return await self._behaviour(args)
        return self._result


# ==============================================================================
# ToolResult
# ==============================================================================


@pytest.mark.unit
def test_failure_helper_builds_a_failed_result() -> None:
    result = ToolResult.failure("文件不存在")
    assert result.ok is False
    assert result.error == "文件不存在"
    assert result.content == "文件不存在"  # 模型读 content，所以失败原因也要在那里


@pytest.mark.unit
def test_a_result_is_immutable() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic 冻结模型的具体异常类型不重要
        ToolResult(ok=True).content = "x"  # type: ignore[misc]


@pytest.mark.unit
def test_exit_code_zero_is_not_a_failure_and_nonzero_is() -> None:
    assert ToolResult(ok=True, exit_code=0).failed is False
    assert ToolResult(ok=True, exit_code=2).failed is True
    assert ToolResult(ok=False).failed is True
    assert ToolResult(ok=True).failed is False


# ==============================================================================
# 注册表
# ==============================================================================


@pytest.mark.unit
def test_empty_names_are_rejected() -> None:
    with pytest.raises(ToolRegistryError, match="名"):
        ToolRegistry([Fake("")])


@pytest.mark.unit
def test_duplicate_names_are_rejected_not_overwritten() -> None:
    with pytest.raises(ToolRegistryError, match="重复"):
        ToolRegistry([Fake("a"), Fake("a")])


@pytest.mark.unit
def test_untrusted_tools_are_refused_by_default() -> None:
    with pytest.raises(ToolRegistryError, match="不可信"):
        ToolRegistry([Fake("web_search", trust="untrusted")])


@pytest.mark.unit
def test_untrusted_tools_need_a_named_grant_when_a_list_is_given() -> None:
    ToolRegistry(
        [Fake("mcp_x", trust="untrusted")], allow_untrusted=True, untrusted_allowlist={"mcp_x"}
    )
    with pytest.raises(ToolRegistryError, match="授权"):
        ToolRegistry(
            [Fake("mcp_y", trust="untrusted")],
            allow_untrusted=True,
            untrusted_allowlist={"mcp_x"},
        )


@pytest.mark.unit
def test_an_isolated_registry_accepts_any_untrusted_tool() -> None:
    ToolRegistry([Fake("web_search", trust="untrusted")], allow_untrusted=True)


@pytest.mark.unit
def test_schemas_are_sorted_by_name_and_stable() -> None:
    forward = ToolRegistry([Fake("b"), Fake("a"), Fake("c")])
    backward = ToolRegistry([Fake("c"), Fake("a"), Fake("b")])
    names = [s.name for s in forward.schemas()]
    assert names == ["a", "b", "c"]
    dump = lambda reg: json.dumps(  # noqa: E731
        [(s.name, s.description, dict(s.parameters)) for s in reg.schemas()], sort_keys=False
    )
    assert dump(forward) == dump(backward)


@pytest.mark.unit
def test_lookup_and_idempotency() -> None:
    registry = ToolRegistry([Fake("view_file", idempotent=True), Fake("bash")])
    assert registry.knows("bash") and not registry.knows("nope")
    assert registry.is_idempotent("view_file") is True
    assert registry.is_idempotent("bash") is False
    assert registry.is_idempotent("nope") is False  # 未知工具按非幂等处理


# ==============================================================================
# 派发器
# ==============================================================================


@pytest.mark.unit
async def test_results_come_back_in_call_order_with_pairs_intact() -> None:
    async def slow(args: Mapping[str, Any]) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(ok=True, content="slow")

    registry = ToolRegistry([Fake("slow", behaviour=slow), Fake("fast")])
    out = await ToolDispatcher(registry).dispatch(
        [("c1", "slow", {}), ("c2", "fast", {}), ("c3", "slow", {})]
    )
    assert [(r.tool_call_id, r.tool_name) for r in out] == [
        ("c1", "slow"),
        ("c2", "fast"),
        ("c3", "slow"),
    ]
    assert [r.result.content for r in out] == ["slow", "fast-ok", "slow"]


@pytest.mark.unit
async def test_a_batch_runs_concurrently() -> None:
    """两个调用互相等待对方启动：串行执行会死锁，被外层超时抓到。"""
    started = [asyncio.Event(), asyncio.Event()]

    def make(i: int) -> Any:
        async def run(args: Mapping[str, Any]) -> ToolResult:
            started[i].set()
            await started[1 - i].wait()
            return ToolResult(ok=True, content=str(i))

        return run

    registry = ToolRegistry([Fake("a", behaviour=make(0)), Fake("b", behaviour=make(1))])
    out = await asyncio.wait_for(
        ToolDispatcher(registry).dispatch([("1", "a", {}), ("2", "b", {})]), timeout=2
    )
    assert [r.result.content for r in out] == ["0", "1"]


@pytest.mark.unit
async def test_every_call_has_its_own_hard_timeout() -> None:
    async def hang(args: Mapping[str, Any]) -> ToolResult:
        await asyncio.sleep(30)
        return ToolResult(ok=True)

    registry = ToolRegistry([Fake("hang", behaviour=hang, timeout_sec=0.05), Fake("fine")])
    out = await asyncio.wait_for(
        ToolDispatcher(registry).dispatch([("1", "hang", {}), ("2", "fine", {})]), timeout=2
    )
    assert out[0].result.ok is False
    assert "超时" in (out[0].result.error or "")
    assert out[1].result.ok is True


@pytest.mark.unit
async def test_a_tool_that_raises_becomes_a_failure_observation() -> None:
    async def boom(args: Mapping[str, Any]) -> ToolResult:
        raise RuntimeError("内部错误 xyz")

    registry = ToolRegistry([Fake("boom", behaviour=boom), Fake("fine")])
    out = await ToolDispatcher(registry).dispatch([("1", "boom", {}), ("2", "fine", {})])
    assert out[0].result.ok is False
    assert "RuntimeError" in (out[0].result.error or "")
    assert out[1].result.ok is True


@pytest.mark.unit
async def test_an_unknown_tool_fails_softly_and_is_untrusted() -> None:
    out = await ToolDispatcher(ToolRegistry([])).dispatch([("1", "ghost", {"x": 1})])
    assert out[0].result.ok is False
    assert "ghost" in (out[0].result.error or "")
    assert out[0].trust == "untrusted"


@pytest.mark.unit
async def test_trust_defaults_to_the_static_declaration_and_a_result_can_override_it() -> None:
    plain = Fake("plain")
    sub = Fake("sub", result=ToolResult(ok=True, content="x", trust="untrusted"))
    out = await ToolDispatcher(ToolRegistry([plain, sub])).dispatch(
        [("1", "plain", {}), ("2", "sub", {})]
    )
    assert [r.trust for r in out] == ["trusted", "untrusted"]


@pytest.mark.unit
async def test_duration_is_measured() -> None:
    async def nap(args: Mapping[str, Any]) -> ToolResult:
        await asyncio.sleep(0.03)
        return ToolResult(ok=True)

    out = await ToolDispatcher(ToolRegistry([Fake("nap", behaviour=nap)])).dispatch(
        [("1", "nap", {})]
    )
    assert out[0].result.duration_ms >= 20


@pytest.mark.unit
async def test_args_are_passed_through_and_the_dispatcher_does_not_judge_permissions() -> None:
    tool = Fake("bash")
    await ToolDispatcher(ToolRegistry([tool])).dispatch([("1", "bash", {"command": "curl x"})])
    assert tool.calls == [{"command": "curl x"}]


@pytest.mark.unit
async def test_an_empty_batch_is_fine() -> None:
    assert await ToolDispatcher(ToolRegistry([])).dispatch([]) == []


@pytest.mark.unit
async def test_cancellation_is_not_swallowed() -> None:
    async def forever(args: Mapping[str, Any]) -> ToolResult:
        await asyncio.sleep(30)
        return ToolResult(ok=True)

    task = asyncio.create_task(
        ToolDispatcher(ToolRegistry([Fake("f", behaviour=forever, timeout_sec=60)])).dispatch(
            [("1", "f", {})]
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
