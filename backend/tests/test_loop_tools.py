"""工具包装层与草稿工具、MCP 代理。

出处：docs/02_runtime/06_tool_execution.md、05_plan_and_scratch.md、
docs/04_tools/04_mcp.md §5.1、决策 0007。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.approval import check_binding, compute_approval_id
from agent_runtime.context import ObservationGovernor, ObservationLimits, ObservationStore
from agent_runtime.core import (
    AgentState,
    AgentTool,
    ApprovalDecision,
    ApprovalRequired,
    Message,
    ToolCall,
    ToolResult,
    apply_update,
    new_state,
)
from agent_runtime.guardrails import LoopLimits, PermissionPolicy, observation_envelope
from agent_runtime.loop import (
    ApprovalHooks,
    McpProxy,
    ToolRunnerDeps,
    apply_scratch,
    described_marker,
    make_tool_runner,
    scratch_tools,
)
from agent_runtime.loop.scratch import Scratch
from mcps import McpCallTool, McpDescribeTool, render_index
from tools.core import ToolDispatcher, ToolRegistry

POLICY = PermissionPolicy(
    read_only_tools=frozenset(
        {"view_file", "update_plan", "note", "mcp_describe", "slow_a", "slow_b"}
    ),
    workspace_write_tools=frozenset({"write_file"}),
    full_permission_tools=frozenset(),
    bash_tools=frozenset({"bash"}),
    full_permission_patterns=(r"\bcurl\b",),
    workspace_write_patterns=(),
    unknown_tool_level="full_permissions",
)
LIMITS = LoopLimits(consecutive_errors_limit=3, identical_fingerprint_limit=3)


class Fake(AgentTool):
    parameters: Mapping[str, Any] = {"type": "object"}

    def __init__(
        self,
        name: str,
        *,
        trust: str = "trusted",
        delay: float = 0.0,
        log: list[str] | None = None,
        ok: bool = True,
        content: str = "",
    ) -> None:
        self.name = name
        self.description = name
        self.trust = trust  # type: ignore[assignment]
        self._delay, self._log, self._ok = delay, log if log is not None else [], ok
        self._content = content or f"{name} done"

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        self._log.append(f"start:{self.name}")
        await asyncio.sleep(self._delay)
        self._log.append(f"end:{self.name}")
        return (
            ToolResult(ok=self._ok, content=self._content)
            if self._ok
            else ToolResult.failure("boom")
        )


def make_runner(
    tmp_path: Path,
    tools: Sequence[AgentTool],
    *,
    hidden: Sequence[str] = (),
    proxy: McpProxy | None = None,
    allowlist: set[str] | None = None,
):  # type: ignore[no-untyped-def]
    untrusted = [t.name for t in tools if t.trust == "untrusted"]
    registry = ToolRegistry(
        tools, allow_untrusted=bool(untrusted), untrusted_allowlist=untrusted or None, hidden=hidden
    )
    limits = ObservationLimits(100000, 40, 40, 10, 3, 5, 100, ("mcp_describe",), 8)
    deps = ToolRunnerDeps(
        dispatcher=ToolDispatcher(registry),
        policy=POLICY,
        limits=LIMITS,
        governor=ObservationGovernor(limits, len, ObservationStore(tmp_path / "art")),
        approval=ApprovalHooks(compute_approval_id, check_binding),
        allowlist=allowlist if allowlist is not None else set(),
        envelope=observation_envelope,
        proxy=proxy,
    )
    return make_tool_runner(deps)


def state_with(calls: list[ToolCall], **fields: Any) -> AgentState:
    base = new_state(
        workspace_id="w",
        workspace_path="/p",
        session_id="s",
        task_id="t",
        task_goal="g",
        permission_level="workspace_write",
        canary_token="c",
    )
    msgs = [
        Message(role="user", content="g"),
        Message(role="assistant", content="", tool_calls=tuple(calls)),
    ]
    return apply_update(base, {"messages": msgs, **fields})


def call(call_id: str, tool: str, /, **args: Any) -> ToolCall:
    return ToolCall(id=call_id, name=tool, args=args)


async def run_batch(
    runner: Any, calls: list[ToolCall], **fields: Any
) -> tuple[AgentState, dict[str, Any]]:
    state = state_with(calls, **fields)
    update = await runner(state, tuple(calls))
    return apply_update(state, update), update


# ==============================================================================
# 草稿工具
# ==============================================================================


@pytest.mark.unit
def test_update_plan_replaces_the_whole_plan() -> None:
    first = apply_scratch(
        Scratch(),
        "update_plan",
        {"items": [{"title": "a"}, {"title": "b", "status": "in_progress"}]},
    )
    assert first.ok and [(p.id, p.status) for p in first.scratch.plan] == [
        (1, "pending"),
        (2, "in_progress"),
    ]
    second = apply_scratch(
        first.scratch, "update_plan", {"items": [{"title": "only", "status": "completed"}]}
    )
    assert [p.title for p in second.scratch.plan] == ["only"]
    assert apply_scratch(second.scratch, "update_plan", {"items": []}).scratch.plan == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "items",
    [
        [{"title": str(i)} for i in range(13)],
        [{"id": 1, "title": "a"}, {"id": 1, "title": "b"}],
        [{"title": "a", "status": "doing"}],
        [{"title": ""}],
        "nope",
    ],
)
def test_update_plan_rejects_bad_input_and_keeps_the_plan(items: Any) -> None:
    before = apply_scratch(Scratch(), "update_plan", {"items": [{"title": "keep"}]}).scratch
    result = apply_scratch(before, "update_plan", {"items": items})
    assert not result.ok and result.scratch == before


@pytest.mark.unit
def test_notes_dedupe_and_cap_without_silent_eviction() -> None:
    s = Scratch()
    s = apply_scratch(s, "note", {"kind": "fact", "text": "x"}).scratch
    again = apply_scratch(s, "note", {"kind": "fact", "text": "x"})
    assert again.ok and "已存在" in again.text and again.scratch.facts == ("x",)
    for i in range(19):
        s = apply_scratch(s, "note", {"kind": "fact", "text": f"f{i}"}).scratch
    full = apply_scratch(s, "note", {"kind": "fact", "text": "one more"})
    assert not full.ok and len(full.scratch.facts) == 20 and "上限" in full.text


@pytest.mark.unit
def test_failed_attempt_needs_all_fields() -> None:
    bad = apply_scratch(Scratch(), "note", {"kind": "failed_attempt", "action": "a"})
    assert not bad.ok
    good = apply_scratch(
        Scratch(),
        "note",
        {"kind": "failed_attempt", "action": "a", "failure_reason": "r", "conclusion": "c"},
    )
    assert good.ok and len(good.scratch.failed_attempts) == 1
    assert not apply_scratch(Scratch(), "note", {"kind": "mystery"}).ok


@pytest.mark.unit
async def test_scratch_calls_go_through_the_wrapper_and_update_state(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, scratch_tools())
    state, update = await run_batch(
        runner,
        [
            call("c1", "update_plan", items=[{"title": "go"}]),
            call("c2", "note", kind="fact", text="f"),
        ],
    )
    assert [p.title for p in state.plan] == ["go"] and state.facts == ("f",)
    assert [m.role for m in state.messages[-2:]] == ["tool", "tool"]
    assert "plan" in update


# ==============================================================================
# 工具包装层
# ==============================================================================


@pytest.mark.unit
async def test_every_call_gets_exactly_one_result_in_order(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("view_file"), Fake("write_file")])
    state, _ = await run_batch(
        runner, [call("c1", "write_file"), call("c2", "view_file"), call("c3", "ghost")]
    )
    tools = [m for m in state.messages if m.role == "tool"]
    assert [m.tool_call_id for m in tools] == ["c1", "c2", "c3"]
    assert "未知工具" in tools[2].content
    assert state.consecutive_errors == 1


@pytest.mark.unit
async def test_read_only_calls_run_concurrently_and_writes_serially(tmp_path: Path) -> None:
    log: list[str] = []
    tools = [
        Fake("slow_a", delay=0.05, log=log),
        Fake("slow_b", delay=0.05, log=log),
        Fake("write_file", delay=0.02, log=log),
    ]
    runner = make_runner(tmp_path, tools)
    await run_batch(runner, [call("1", "slow_a"), call("2", "slow_b"), call("3", "write_file")])
    assert log[:2] == ["start:slow_a", "start:slow_b"]  # 只读并发
    assert log.index("start:write_file") > log.index("end:slow_b")  # 写在之后串行


@pytest.mark.unit
async def test_escalation_without_a_decision_suspends_before_any_side_effect(
    tmp_path: Path,
) -> None:
    log: list[str] = []
    runner = make_runner(tmp_path, [Fake("bash", log=log), Fake("view_file", log=log)])
    with pytest.raises(ApprovalRequired) as exc:
        await run_batch(runner, [call("c1", "view_file"), call("c2", "bash", command="curl x")])
    assert exc.value.payload.call_ids == ("c2",)
    assert log == []  # 挂起前没有任何调用被执行


@pytest.mark.unit
async def test_a_rejection_blocks_only_the_escalated_call_and_is_not_an_error(
    tmp_path: Path,
) -> None:
    runner = make_runner(tmp_path, [Fake("bash"), Fake("view_file")])
    state = state_with([call("c1", "view_file"), call("c2", "bash", command="curl x")])
    pending = None
    try:
        await runner(state, tuple(state.messages[-1].tool_calls))
    except ApprovalRequired as request:
        pending = request.payload
    assert pending is not None
    decided = apply_update(
        state,
        {
            "pending_approval": pending,
            "approval_decision": ApprovalDecision(
                approval_id=pending.approval_id, approved=False, reason="no"
            ),
        },
    )
    update = await runner(decided, decided.messages[-1].tool_calls)
    done = apply_update(decided, update)
    results = {m.tool_call_id: m.content for m in done.messages if m.role == "tool"}
    assert "已被人工审核拒绝：no" in results["c2"] and "view_file done" in results["c1"]
    assert done.consecutive_errors == 0  # 人工拒绝不是工具失败
    assert done.pending_approval is None and done.approval_decision is None


@pytest.mark.unit
async def test_an_always_decision_whitelists_the_signature(tmp_path: Path) -> None:
    allow: set[str] = set()
    runner = make_runner(tmp_path, [Fake("bash")], allowlist=allow)
    state = state_with([call("c1", "bash", command="curl x")])
    with pytest.raises(ApprovalRequired) as exc:
        await runner(state, state.messages[-1].tool_calls)
    decided = apply_update(
        state,
        {
            "pending_approval": exc.value.payload,
            "approval_decision": ApprovalDecision(
                approval_id=exc.value.payload.approval_id, approved=True, scope="always"
            ),
        },
    )
    await runner(decided, decided.messages[-1].tool_calls)
    assert allow
    again = state_with([call("c9", "bash", command="curl x")])
    await runner(again, again.messages[-1].tool_calls)  # 同签名不再询问


@pytest.mark.unit
async def test_identical_calls_are_blocked_as_a_whole_batch_and_counted(tmp_path: Path) -> None:
    log: list[str] = []
    runner = make_runner(tmp_path, [Fake("view_file", log=log)])
    history: tuple[str, ...] = ()
    state = None
    for i in range(3):
        state, _update = await run_batch(
            runner, [call(f"c{i}", "view_file", path="a")], fingerprint_history=history
        )
        history = state.fingerprint_history
    assert state is not None
    assert state.consecutive_blocks == 1 and "已拦截" in state.messages[-1].content
    assert log.count("start:view_file") == 2  # 第三次整批被拦截，没有派发
    assert state.consecutive_errors == 1


@pytest.mark.unit
async def test_a_normal_batch_resets_the_block_counter(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("view_file")])
    state, _ = await run_batch(runner, [call("c1", "view_file", path="x")], consecutive_blocks=2)
    assert state.consecutive_blocks == 0


@pytest.mark.unit
async def test_malformed_arguments_are_not_executed_or_judged(tmp_path: Path) -> None:
    log: list[str] = []
    runner = make_runner(tmp_path, [Fake("bash", log=log)])
    state, _ = await run_batch(
        runner, [ToolCall(id="c1", name="bash", args={"_raw": '{"command": "curl'})]
    )
    assert (
        log == [] and "参数无法使用" in state.messages[-1].content and state.consecutive_errors == 1
    )


@pytest.mark.unit
async def test_consecutive_errors_append_a_notice_after_the_tool_messages(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("view_file", ok=False)])
    state, _ = await run_batch(
        runner,
        [call("c1", "view_file", a=1), call("c2", "view_file", a=2), call("c3", "view_file", a=3)],
    )
    assert state.messages[-1].role == "user" and "连续 3 次" in state.messages[-1].content
    assert [m.role for m in state.messages[-4:-1]] == ["tool"] * 3


@pytest.mark.unit
async def test_verification_follows_edits_and_commands(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("write_file"), Fake("bash")])
    state, _ = await run_batch(runner, [call("c1", "write_file", path="a")])
    assert state.verification == "unchecked"
    state, _ = await run_batch(
        runner, [call("c2", "bash", command="pytest")], verification="unchecked"
    )
    assert state.verification == "passed"


@pytest.mark.security
@pytest.mark.unit
async def test_untrusted_results_are_wrapped_after_truncation(tmp_path: Path) -> None:
    runner = make_runner(tmp_path, [Fake("mcp__x__t", trust="untrusted", content="payload")])
    state = state_with([call("c1", "mcp__x__t")])
    # 未知工具默认 full_permissions：先批准再看信封
    with pytest.raises(ApprovalRequired) as exc:
        await runner(state, state.messages[-1].tool_calls)
    decided = apply_update(
        state,
        {
            "pending_approval": exc.value.payload,
            "approval_decision": ApprovalDecision(
                approval_id=exc.value.payload.approval_id, approved=True
            ),
        },
    )
    done = apply_update(decided, await runner(decided, decided.messages[-1].tool_calls))
    assert (
        'trust="untrusted"' in done.messages[-1].content and "payload" in done.messages[-1].content
    )


# ==============================================================================
# MCP 代理（决策 0007）
# ==============================================================================

DEFERRED = "mcp__docs__search"
PINNED = "mcp__docs__ping"


def mcp_runner(tmp_path: Path, log: list[str] | None = None, **kw: Any):  # type: ignore[no-untyped-def]
    tools: list[AgentTool] = [
        Fake(DEFERRED, trust="untrusted", log=log),
        Fake(PINNED, trust="untrusted", log=log),
    ]
    tools.append(
        McpDescribeTool(
            lambda n: next((t.schema() for t in tools if t.name == n and n == DEFERRED), None),
            lambda: [DEFERRED],
        )
    )
    tools.append(McpCallTool())
    return make_runner(
        tmp_path, tools, hidden=[DEFERRED], proxy=McpProxy([DEFERRED], [PINNED]), **kw
    )


def described(state_calls: list[ToolCall]) -> AgentState:
    """transcript 里已有一次 mcp_describe 取回目标定义。"""
    desc = Message(
        role="tool",
        content=f"{described_marker(DEFERRED)}>\nschema",
        tool_call_id="d1",
        name="mcp_describe",
    )
    first = Message(
        role="assistant", content="", tool_calls=(call("d1", "mcp_describe", names=[DEFERRED]),)
    )
    base = new_state(
        workspace_id="w",
        workspace_path="/p",
        session_id="s",
        task_id="t",
        task_goal="g",
        permission_level="workspace_write",
        canary_token="c",
    )
    return apply_update(
        base,
        {
            "messages": [
                Message(role="user", content="g"),
                first,
                desc,
                Message(role="assistant", content="", tool_calls=tuple(state_calls)),
            ]
        },
    )


@pytest.mark.unit
async def test_mcp_call_requires_a_prior_describe(tmp_path: Path) -> None:
    log: list[str] = []
    runner = mcp_runner(tmp_path, log)
    s = state_with([call("c1", "mcp_call", name=DEFERRED, arguments={"q": "x"})])
    done = apply_update(s, await runner(s, s.messages[-1].tool_calls))
    assert (
        "mcp_describe" in done.messages[-1].content and log == [] and done.consecutive_errors == 1
    )


@pytest.mark.unit
async def test_mcp_call_dispatches_the_target_after_describe(tmp_path: Path) -> None:
    log: list[str] = []
    runner = mcp_runner(tmp_path, log)
    calls = [call("c1", "mcp_call", name=DEFERRED, arguments={"q": "x"})]
    s = described(calls)
    with pytest.raises(ApprovalRequired) as exc:  # 未知 MCP 工具默认 full_permissions：按目标判定
        await runner(s, tuple(calls))
    assert "mcp_call" not in exc.value.payload.command
    ok = apply_update(
        s,
        {
            "pending_approval": exc.value.payload,
            "approval_decision": ApprovalDecision(
                approval_id=exc.value.payload.approval_id, approved=True
            ),
        },
    )
    done = apply_update(ok, await runner(ok, tuple(calls)))
    assert log == [f"start:{DEFERRED}", f"end:{DEFERRED}"]
    assert done.messages[-1].name == "mcp_call"  # transcript 里保留模型原始发出的代理调用


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "why"),
    [
        ({"name": PINNED, "arguments": {}}, "直接调用"),
        ({"name": "mcp__nope__x", "arguments": {}}, "索引里没有"),
        ({"name": DEFERRED, "arguments": "not-an-object"}, "必须是对象"),
        ({"arguments": {}}, "name"),
    ],
)
async def test_mcp_call_refuses_unresolvable_targets_without_dispatching(
    tmp_path: Path, args: dict[str, Any], why: str
) -> None:
    log: list[str] = []
    runner = mcp_runner(tmp_path, log)
    s = state_with([call("c1", "mcp_call", **args)])
    done = apply_update(s, await runner(s, s.messages[-1].tool_calls))
    assert why in done.messages[-1].content and log == [] and done.consecutive_errors == 1


@pytest.mark.unit
async def test_mcp_describe_returns_definitions_and_server_listings(tmp_path: Path) -> None:
    runner = mcp_runner(tmp_path)
    s = state_with([call("c1", "mcp_describe", names=[DEFERRED, "mcp__docs", "ghost"])])
    done = apply_update(s, await runner(s, s.messages[-1].tool_calls))
    text = done.messages[-1].content
    assert described_marker(DEFERRED) in text and "<mcp_server" in text and "未找到" in text
    assert done.messages[-1].content.count("untrusted") >= 1  # 第三方内容，带不可信信封


@pytest.mark.unit
def test_hidden_tools_are_dispatchable_but_not_exported() -> None:
    registry = ToolRegistry([Fake("a"), Fake("b")], hidden=["b"])
    assert [s.name for s in registry.schemas()] == ["a"]
    assert registry.knows("b") and registry.hidden_schema("b") is not None
    with pytest.raises(Exception, match="hidden"):
        ToolRegistry([Fake("a")], hidden=["zzz"])


@pytest.mark.unit
def test_the_index_is_stable_sorted_and_untrusted() -> None:
    entries = [
        ("docs", "mcp__docs__b", "Second. More."),
        ("docs", "mcp__docs__a", "First one. Extra."),
    ]
    text = render_index(entries, max_listed=10, desc_chars=80)
    assert text == render_index(list(reversed(entries)), max_listed=10, desc_chars=80)
    assert text.index("mcp__docs__a") < text.index("mcp__docs__b")
    assert 'trust="untrusted"' in text and "Extra" not in text  # 只取首句
    assert render_index([], max_listed=10, desc_chars=80) == ""


@pytest.mark.unit
def test_an_oversized_index_lists_servers_only() -> None:
    entries = [("docs", f"mcp__docs__t{i}", "d") for i in range(5)]
    text = render_index(entries, max_listed=3, desc_chars=80)
    assert "mcp__docs__t0" not in text and "5 个工具" in text
