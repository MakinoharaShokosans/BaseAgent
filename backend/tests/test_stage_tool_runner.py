"""tool_runner 阶段：权限闸门、派发前标记、派发、观察值治理、指纹与连续错误护栏。

出处：docs/02_runtime/02_main_loop.md §5；04_approval.md；05_security/01_permission_tiers.md。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.approval import (
    ApprovalMismatchError,
    bind_decision,
    check_binding,
    compute_approval_id,
)
from agent_runtime.context import ObservationGovernor, ObservationLimits, ObservationStore
from agent_runtime.core import (
    AgentState,
    ApprovalDecision,
    ApprovalRequired,
    Message,
    ToolCall,
    ToolResult,
    apply_update,
    check_invariants,
)
from agent_runtime.guardrails import (
    LoopLimits,
    action_signature,
    compute_fingerprint,
)
from agent_runtime.loop import next_phase
from agent_runtime.loop.stages import ApprovalHooks, ToolRunnerDeps, make_tool_runner_stage
from stage_support import Events, MakeState, with_state
from test_permissions import POLICY
from test_tools_core import Fake
from tools.core import ToolDispatcher, ToolRegistry

HOOKS = ApprovalHooks(compute_approval_id, check_binding)
LIMITS = LoopLimits(consecutive_errors_limit=3, identical_fingerprint_limit=3)
OBS = ObservationLimits(
    max_tokens=200,
    head_lines=3,
    tail_lines=4,
    keyword_lines_limit=5,
    json_outline_max_depth=3,
    json_outline_array_items=2,
    scalar_preview_chars=10,
    exempt_tools=(),
    exempt_multiplier=4,
)


class Rig:
    """把一次 tool_runner 需要的假件装在一起，并按时间顺序记录提交与执行。"""

    def __init__(self, tmp_path: Path, tools: list[Fake] | None = None, **kw: Any) -> None:
        self.tools = tools or [
            Fake("view_file", idempotent=True),
            Fake("write_file"),
            Fake("bash"),
        ]
        self.by_name = {t.name: t for t in self.tools}
        self.log: list[str] = []
        self.commits: list[AgentState] = []
        self.events = Events()
        self.allowlist: set[str] = set()
        for tool in self.tools:
            tool._behaviour = self._wrap(tool)
        registry = ToolRegistry(self.tools)
        self.deps = ToolRunnerDeps(
            dispatcher=ToolDispatcher(registry),
            policy=POLICY,
            limits=kw.pop("limits", LIMITS),
            governor=ObservationGovernor(OBS, len, ObservationStore(tmp_path / "artifacts")),
            approval=HOOKS,
            commit=self._commit,
            allowlist=self.allowlist,
            emit=self.events,
            **kw,
        )
        self.stage = make_tool_runner_stage(self.deps)

    def _wrap(self, tool: Fake) -> Any:
        original = tool._result

        async def run(args: Mapping[str, Any]) -> ToolResult:
            self.log.append(f"run:{tool.name}")
            return original

        return run

    async def _commit(self, state: AgentState) -> None:
        self.log.append("commit")
        check_invariants(state)
        self.commits.append(state)

    async def run(self, state: AgentState) -> tuple[dict[str, object], AgentState]:
        update = dict(await self.stage(state))
        return update, apply_update(state, update)


def batch(
    make_state: MakeState, *calls: tuple[str, str, Mapping[str, Any]], **fields: Any
) -> AgentState:
    base: dict[str, Any] = {
        "phase": "tool_runner",
        "step_count": 1,
        "messages": (
            Message(role="user", content="fix"),
            Message(
                role="assistant",
                content="",
                tool_calls=tuple(ToolCall(id=i, name=n, args=a) for i, n, a in calls),
            ),
        ),
    }
    level = fields.pop("permission_level", "workspace_write")
    return with_state(make_state(permission_level=level), **{**base, **fields})


def tool_messages(state: AgentState) -> list[Message]:
    return [m for m in state.messages if m.role == "tool"]


# ==============================================================================
# 基本派发
# ==============================================================================


@pytest.mark.unit
async def test_nothing_to_do_without_tool_calls(make_state: MakeState, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    state = with_state(make_state(), messages=(Message(role="assistant", content="结论"),))
    assert await rig.stage(state) == {}
    assert rig.commits == []


@pytest.mark.unit
async def test_every_call_gets_a_paired_tool_message_in_order(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(
        make_state, ("c1", "view_file", {"path": "a"}), ("c2", "write_file", {"path": "b"})
    )
    _, after = await rig.run(state)

    msgs = tool_messages(after)
    assert [(m.tool_call_id, m.name) for m in msgs] == [("c1", "view_file"), ("c2", "write_file")]
    assert [m.content for m in msgs] == ["view_file-ok", "write_file-ok"]
    assert next_phase("tool_runner", after) == "planner"
    after_clean = with_state(after, phase="planner")
    check_invariants(after_clean)


@pytest.mark.unit
async def test_the_stage_never_writes_the_phase_or_the_step_count(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    update, _ = await rig.run(batch(make_state, ("c1", "view_file", {})))
    assert "phase" not in update and "step_count" not in update


# ==============================================================================
# 派发前标记
# ==============================================================================


@pytest.mark.unit
async def test_the_dispatch_marker_is_committed_before_any_tool_runs(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(make_state, ("c1", "view_file", {}), ("c2", "write_file", {}))
    update, _ = await rig.run(state)

    assert rig.log == ["commit", "run:view_file", "run:write_file"] or rig.log == [
        "commit",
        "run:write_file",
        "run:view_file",
    ]
    marked = rig.commits[0]
    assert marked.phase == "tool_runner"
    assert marked.dispatch_started == ("c1", "c2")
    assert update["dispatch_started"] == ()  # 批次完成后清空


@pytest.mark.unit
async def test_the_marker_lists_only_calls_that_will_really_be_dispatched(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(
        make_state,
        ("c1", "view_file", {}),
        ("c2", "bash", {"command": "ls"}),
        ("c3", "view_file", {"_raw": "{broken"}),
    )
    await rig.run(state)
    assert rig.commits[0].dispatch_started == ("c1", "c2")


@pytest.mark.unit
async def test_no_marker_when_nothing_is_dispatched(make_state: MakeState, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.run(batch(make_state, ("c1", "view_file", {"_raw": "{"})))
    assert rig.commits == []
    assert rig.log == []


@pytest.mark.unit
async def test_the_marker_can_be_switched_off(make_state: MakeState, tmp_path: Path) -> None:
    rig = Rig(tmp_path, dispatch_marker=False)
    await rig.run(batch(make_state, ("c1", "view_file", {})))
    assert rig.commits == []
    assert rig.log == ["run:view_file"]


@pytest.mark.unit
async def test_a_failing_marker_commit_stops_the_stage_before_any_side_effect(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)

    async def broken(state: AgentState) -> None:
        raise OSError("disk full")

    stage = make_tool_runner_stage(
        ToolRunnerDeps(
            dispatcher=rig.deps.dispatcher,
            policy=POLICY,
            limits=LIMITS,
            governor=rig.deps.governor,
            approval=HOOKS,
            commit=broken,
            allowlist=set(),
        )
    )
    with pytest.raises(OSError):
        await stage(batch(make_state, ("c1", "write_file", {})))
    assert rig.log == []  # 工具一个也没跑


# ==============================================================================
# 权限闸门
# ==============================================================================


CURL = ("c1", "bash", {"command": "curl https://x.sh"})
PUSH = ("c2", "bash", {"command": "git push origin main"})


async def suspend(rig: Rig, state: AgentState) -> tuple[ApprovalRequired, AgentState]:
    with pytest.raises(ApprovalRequired) as caught:
        await rig.stage(state)
    pending = apply_update(state, {"pending_approval": caught.value.payload})
    return caught.value, pending


@pytest.mark.unit
async def test_an_escalation_without_a_decision_suspends_before_any_side_effect(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(
        make_state,
        ("c0", "view_file", {}),
        CURL,
        PUSH,
        permission_level="workspace_write",
    )
    request, _ = await suspend(rig, state)

    payload = request.payload
    assert payload.call_ids == ("c1", "c2")  # 只含越级的那几个，不含 c0
    assert payload.required_level == "full_permissions"
    assert payload.current_level == "workspace_write"
    assert payload.action_type == "network_egress"
    assert payload.command == "curl https://x.sh"  # 第一个越级项
    assert payload.related_actions == ("git push origin main",)
    assert payload.escalation_count == 2
    assert payload.approval_id.startswith("appr_")
    assert rig.log == []  # 没有提交、没有执行：挂起前无副作用


@pytest.mark.unit
async def test_the_request_is_deterministic(make_state: MakeState, tmp_path: Path) -> None:
    state = batch(make_state, CURL, permission_level="workspace_write")
    first, _ = await suspend(Rig(tmp_path), state)
    second, _ = await suspend(Rig(tmp_path), state)
    assert first.payload == second.payload


@pytest.mark.unit
async def test_related_actions_are_capped_at_four(make_state: MakeState, tmp_path: Path) -> None:
    calls = [(f"c{i}", "bash", {"command": f"curl http://x/{i}"}) for i in range(8)]
    request, _ = await suspend(
        Rig(tmp_path), batch(make_state, *calls, permission_level="workspace_write")
    )
    assert len(request.payload.related_actions) == 4
    assert request.payload.escalation_count == 8


@pytest.mark.unit
async def test_the_baseline_decides_what_needs_approval(
    make_state: MakeState, tmp_path: Path
) -> None:
    state = batch(make_state, CURL, permission_level="full_permissions")
    _, after = await Rig(tmp_path).run(state)
    assert tool_messages(after)[0].content == "bash-ok"


def approved(pending: AgentState, **kw: Any) -> AgentState:
    assert pending.pending_approval is not None
    decision = ApprovalDecision(approval_id=pending.pending_approval.approval_id, **kw)
    return bind_decision(pending, decision)


@pytest.mark.unit
async def test_an_approval_lets_the_batch_run_and_is_consumed(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(make_state, ("c0", "view_file", {}), CURL, permission_level="workspace_write")
    _, pending = await suspend(rig, state)

    update, after = await rig.run(approved(pending, approved=True))

    assert [m.content for m in tool_messages(after)] == ["view_file-ok", "bash-ok"]
    assert after.pending_approval is None and after.approval_decision is None
    assert update["pending_approval"] is None and update["approval_decision"] is None
    # 标记提交里也已经没有悬空的审批：否则崩溃恢复会把它当成“正挂起在审批上”而不处理
    assert rig.commits[0].pending_approval is None
    assert rig.commits[0].approval_decision is None
    assert [e for e in rig.events.names() if e.startswith("guard.approval")] == [
        "guard.approval_decided"
    ]
    payload = next(p for n, p in rig.events.items if n == "guard.approval_decided")
    assert payload["approved"] is True and payload["scope"] == "once"


@pytest.mark.unit
async def test_a_rejection_synthesizes_failures_only_for_the_escalated_calls(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(
        make_state,
        ("c0", "view_file", {}),
        CURL,
        permission_level="workspace_write",
        consecutive_errors=1,
    )
    _, pending = await suspend(rig, state)

    _, after = await rig.run(approved(pending, approved=False, reason="不允许联网"))

    msgs = tool_messages(after)
    assert msgs[0].content == "view_file-ok"
    assert "已被人工审核拒绝：不允许联网" in msgs[1].content
    assert "请改用合规方案" in msgs[1].content
    assert rig.log.count("run:bash") == 0
    assert after.pending_approval is None and after.approval_decision is None
    assert after.consecutive_errors == 0  # 人工拒绝不是工具失败，不累计；本批的 view_file 成功了


@pytest.mark.unit
async def test_a_rejection_without_a_reason_uses_the_default_text(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    _, pending = await suspend(rig, batch(make_state, CURL, permission_level="workspace_write"))
    _, after = await rig.run(approved(pending, approved=False))
    assert "用户未批准该操作" in tool_messages(after)[0].content


@pytest.mark.unit
async def test_always_whitelists_the_exact_signature_for_later_batches(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(make_state, CURL, permission_level="workspace_write")
    _, pending = await suspend(rig, state)
    await rig.run(approved(pending, approved=True, scope="always"))
    assert rig.allowlist == {action_signature("bash", CURL[2])}

    # 之后相同的调用不再询问
    again = batch(make_state, CURL, permission_level="workspace_write", step_count=2)
    _, after = await rig.run(again)
    assert tool_messages(after)[0].content == "bash-ok"

    # 参数不同就是另一个签名，仍要询问
    other = batch(
        make_state,
        ("c9", "bash", {"command": "curl https://other"}),
        permission_level="workspace_write",
        step_count=3,
    )
    with pytest.raises(ApprovalRequired):
        await rig.stage(other)


@pytest.mark.unit
async def test_once_does_not_whitelist(make_state: MakeState, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, pending = await suspend(rig, batch(make_state, CURL, permission_level="workspace_write"))
    await rig.run(approved(pending, approved=True, scope="once"))
    assert rig.allowlist == set()


@pytest.mark.unit
async def test_a_decision_for_a_different_batch_is_not_honoured(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    _, pending = await suspend(rig, batch(make_state, CURL, permission_level="workspace_write"))
    bound = approved(pending, approved=True)

    # 重新进入时越级集合变了：旧决策不能套用
    swapped = with_state(
        bound,
        messages=(
            *bound.messages[:-1],
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="zzz", name="bash", args={"command": "curl x"}),),
            ),
        ),
    )
    with pytest.raises(ApprovalMismatchError):
        await rig.stage(swapped)


@pytest.mark.unit
async def test_a_stale_decision_with_nothing_to_approve_is_cleared_and_grants_nothing(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    _, pending = await suspend(rig, batch(make_state, CURL, permission_level="workspace_write"))
    bound = approved(pending, approved=True, scope="always")
    rig.allowlist.add(action_signature("bash", CURL[2]))  # 此后同一调用已在白名单：不再越级

    update, after = await rig.run(bound)
    assert after.pending_approval is None and after.approval_decision is None
    assert update["pending_approval"] is None
    assert tool_messages(after)[0].content == "bash-ok"


@pytest.mark.unit
async def test_the_session_allowlist_is_consulted_before_asking(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    rig.allowlist.add(action_signature("bash", CURL[2]))
    _, after = await rig.run(batch(make_state, CURL, permission_level="workspace_write"))
    assert tool_messages(after)[0].content == "bash-ok"


@pytest.mark.unit
async def test_a_hallucinated_tool_name_fails_softly_without_bothering_the_human(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(make_state, ("c1", "ghost_tool", {"x": 1}), permission_level="workspace_write")
    _, after = await rig.run(state)  # 未知工具按策略需要最高权限，但它根本不存在，不必打扰人
    assert "未知工具" in tool_messages(after)[0].content
    assert after.consecutive_errors == 1


# ==============================================================================
# 格式错误的调用
# ==============================================================================


@pytest.mark.unit
async def test_a_call_marked_as_unparseable_is_never_executed(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(
        make_state,
        ("c1", "bash", {"_raw": '{"command": "rm -rf /'}),
        ("c2", "view_file", {"path": "a"}),
    )
    _, after = await rig.run(state)

    msgs = tool_messages(after)
    assert "未执行" in msgs[0].content and "重新发起" in msgs[0].content
    assert msgs[1].content == "view_file-ok"
    assert "run:bash" not in rig.log
    assert after.consecutive_errors == 1  # 参数无法使用是模型的错，计为失败；view_file 成功不抵消
    assert rig.by_name["bash"].calls == []


# ==============================================================================
# 重复指纹
# ==============================================================================


@pytest.mark.unit
async def test_the_fingerprint_history_accumulates_and_slides(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    state = batch(make_state, ("c1", "view_file", {"path": "a"}))
    _, after = await rig.run(state)
    assert after.fingerprint_history == (compute_fingerprint("view_file", {"path": "a"}),)

    long_history = tuple(f"h{i}" for i in range(10))
    _, after = await rig.run(with_state(state, fingerprint_history=long_history))
    assert len(after.fingerprint_history) == LIMITS.window
    assert after.fingerprint_history[-1] == compute_fingerprint("view_file", {"path": "a"})


@pytest.mark.unit
async def test_a_batch_that_completes_a_streak_is_blocked_whole_and_the_model_is_told(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    same = ("view_file", {"path": "a"})
    state = batch(
        make_state,
        ("c0", "write_file", {"path": "b"}),  # 不同的调用也在同一批里：整批一起拦
        ("c1", *same),
        ("c2", *same),
        ("c3", *same),
    )
    update, after = await rig.run(state)

    msgs = tool_messages(after)
    assert [m.tool_call_id for m in msgs] == ["c0", "c1", "c2", "c3"]
    assert all("已拦截" in m.content for m in msgs)
    assert rig.log == []  # 不派发，也不写标记
    assert rig.commits == []
    notice = after.messages[-1]
    assert notice.role == "user" and notice.content.startswith("[系统护栏]")
    assert "完全相同" in notice.content
    assert after.consecutive_errors == 4  # 被拦截的调用计为失败
    assert "guard.loop_blocked" in rig.events.names()
    assert update["dispatch_started"] == ()


@pytest.mark.unit
async def test_a_different_call_in_between_breaks_the_streak(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    same = compute_fingerprint("view_file", {"path": "a"})
    state = batch(
        make_state,
        ("c1", "view_file", {"path": "a"}),
        fingerprint_history=(same, compute_fingerprint("bash", {}), same),
    )
    _, after = await rig.run(state)
    assert tool_messages(after)[0].content == "view_file-ok"


@pytest.mark.unit
async def test_a_blocked_batch_keeps_pairs_intact_even_with_a_pending_escalation(
    make_state: MakeState, tmp_path: Path
) -> None:
    """基线允许的调用若命中重复指纹，同样整批拦截，且配对完整。"""
    rig = Rig(tmp_path)
    same = compute_fingerprint("bash", CURL[2])
    state = batch(
        make_state, CURL, permission_level="full_permissions", fingerprint_history=(same, same)
    )
    _, after = await rig.run(state)
    assert "已拦截" in tool_messages(after)[0].content
    check_invariants(with_state(after, phase="planner"))


# ==============================================================================
# 连续错误
# ==============================================================================


def failing_rig(tmp_path: Path, **result: Any) -> Rig:
    return Rig(
        tmp_path,
        tools=[
            Fake("view_file", idempotent=True, result=ToolResult(ok=True, content="fine")),
            Fake("bash", result=ToolResult(**result)),
        ],
    )


@pytest.mark.unit
async def test_failures_accumulate_and_success_resets(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = failing_rig(tmp_path, ok=False, content="boom", error="boom")
    state = batch(make_state, ("c1", "bash", {"command": "make"}), consecutive_errors=1)
    _, after = await rig.run(state)
    assert after.consecutive_errors == 2
    assert after.messages[-1].role == "tool"  # 还没到阈值，没有通知

    _, healed = await rig.run(batch(make_state, ("c1", "view_file", {}), consecutive_errors=2))
    assert healed.consecutive_errors == 0


@pytest.mark.unit
async def test_reaching_the_limit_appends_a_replan_notice_after_the_tool_messages(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = failing_rig(tmp_path, ok=False, content="boom", error="boom")
    state = batch(make_state, ("c1", "bash", {"command": "make"}), consecutive_errors=2)
    update, after = await rig.run(state)

    assert after.consecutive_errors == 3
    notice = after.messages[-1]
    assert notice.role == "user"
    assert "[系统护栏]" in notice.content and "连续 3 次" in notice.content
    assert after.messages[-2].role == "tool"
    assert "guard.replan_notice" in rig.events.names()
    check_invariants(with_state(after, phase="planner"))
    assert update["consecutive_errors"] == 3


@pytest.mark.unit
async def test_a_nonzero_exit_code_counts_as_a_failure_even_when_the_tool_says_ok(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = failing_rig(tmp_path, ok=True, content="tests failed", exit_code=1)
    _, after = await rig.run(batch(make_state, ("c1", "bash", {"command": "pytest"})))
    assert after.consecutive_errors == 1


@pytest.mark.unit
async def test_a_batch_with_any_success_still_counts_its_failures(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = failing_rig(tmp_path, ok=False, content="boom", error="boom")
    state = batch(make_state, ("c1", "view_file", {}), ("c2", "bash", {"command": "make"}))
    _, after = await rig.run(state)
    assert after.consecutive_errors == 1  # 按批内失败个数累加


@pytest.mark.unit
async def test_a_tool_that_times_out_or_raises_is_just_a_failure(
    make_state: MakeState, tmp_path: Path
) -> None:
    async def boom(args: Mapping[str, Any]) -> ToolResult:
        raise RuntimeError("崩了")

    rig = Rig(tmp_path, tools=[Fake("bash", behaviour=boom)])
    rig.tools[0]._behaviour = boom
    _, after = await rig.run(batch(make_state, ("c1", "bash", {"command": "ls"})))
    assert "RuntimeError" in tool_messages(after)[0].content
    assert after.consecutive_errors == 1


# ==============================================================================
# 观察值治理
# ==============================================================================


@pytest.mark.unit
async def test_a_big_observation_is_shortened_saved_and_registered(
    make_state: MakeState, tmp_path: Path
) -> None:
    big = "\n".join(f"line {i:04d} some output" for i in range(200))
    rig = Rig(tmp_path, tools=[Fake("bash", result=ToolResult(ok=True, content=big))])
    update, after = await rig.run(
        batch(make_state, ("c1", "bash", {"command": "ls"}), step_count=4)
    )

    content = tool_messages(after)[0].content
    assert len(content) < len(big)
    assert "artifact://obs_step_4_bash" in content
    assert set(after.artifacts) == {"obs_step_4_bash"}
    assert Path(after.artifacts["obs_step_4_bash"]).read_text(encoding="utf-8") == big  # noqa: ASYNC240
    assert update["artifacts"] == dict(after.artifacts)


@pytest.mark.unit
async def test_a_small_observation_is_untouched_and_leaves_no_artifact(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path)
    _, after = await rig.run(batch(make_state, ("c1", "view_file", {})))
    assert after.artifacts == {}
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.unit
async def test_two_big_outputs_in_one_step_get_distinct_handles_and_existing_ones_survive(
    make_state: MakeState, tmp_path: Path
) -> None:
    big = "\n".join(f"row {i:04d} payload payload" for i in range(200))
    rig = Rig(tmp_path, tools=[Fake("bash", result=ToolResult(ok=True, content=big))])
    state = batch(
        make_state,
        ("c1", "bash", {"command": "a"}),
        ("c2", "bash", {"command": "b"}),
        step_count=2,
        artifacts={"obs_step_1_bash": "/old"},
    )
    _, after = await rig.run(state)
    assert set(after.artifacts) == {"obs_step_1_bash", "obs_step_2_bash", "obs_step_2_bash_2"}


@pytest.mark.unit
async def test_a_failure_observation_falls_back_to_error_when_content_is_empty(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(
        tmp_path, tools=[Fake("bash", result=ToolResult(ok=False, content="", error="没有权限"))]
    )
    _, after = await rig.run(batch(make_state, ("c1", "bash", {"command": "ls"})))
    assert tool_messages(after)[0].content == "没有权限"


@pytest.mark.unit
async def test_an_empty_successful_observation_stays_empty_but_valid(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, tools=[Fake("bash", result=ToolResult(ok=True, content=""))])
    _, after = await rig.run(batch(make_state, ("c1", "bash", {"command": "true"})))
    assert tool_messages(after)[0].content == ""  # 空内容也必须有配对消息


# ==============================================================================
# 信任
# ==============================================================================


@pytest.mark.unit
async def test_an_untrusted_observation_goes_through_the_envelope_hook(
    make_state: MakeState, tmp_path: Path
) -> None:
    seen: list[tuple[str, str]] = []

    def envelope(tool: str, trust: str, text: str) -> str:
        seen.append((tool, trust))
        return f"<obs trust={trust}>{text}</obs>"

    rig = Rig(
        tmp_path,
        tools=[Fake("sub", result=ToolResult(ok=True, content="外来内容", trust="untrusted"))],
        envelope=envelope,
    )
    state = batch(make_state, ("c1", "sub", {}), permission_level="full_permissions")
    _, after = await rig.run(state)
    assert tool_messages(after)[0].content == "<obs trust=untrusted>外来内容</obs>"
    assert seen == [("sub", "untrusted")]


@pytest.mark.unit
async def test_the_envelope_wraps_the_shortened_text_so_its_warning_is_never_cut(
    make_state: MakeState, tmp_path: Path
) -> None:
    big = "\n".join(f"line {i:04d} some output" for i in range(200))
    rig = Rig(
        tmp_path,
        tools=[Fake("sub", result=ToolResult(ok=True, content=big, trust="untrusted"))],
        envelope=lambda tool, trust, text: f"[不可信]{text}[/不可信]",
    )
    _, after = await rig.run(
        batch(make_state, ("c1", "sub", {}), permission_level="full_permissions")
    )
    content = tool_messages(after)[0].content
    assert content.startswith("[不可信]") and content.endswith("[/不可信]")
    assert "省略" in content


@pytest.mark.unit
async def test_trusted_observations_are_not_wrapped(make_state: MakeState, tmp_path: Path) -> None:
    rig = Rig(tmp_path, envelope=lambda tool, trust, text: f"WRAPPED{text}")
    _, after = await rig.run(batch(make_state, ("c1", "view_file", {})))
    assert tool_messages(after)[0].content == "view_file-ok"


# ==============================================================================
# 任务绑定与工具自带的产物
# ==============================================================================


@pytest.mark.unit
async def test_the_bind_hook_sees_the_state_before_anything_is_dispatched(
    make_state: MakeState, tmp_path: Path
) -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    rig = Rig(tmp_path)
    rig.deps = dataclasses.replace(
        rig.deps, bind=lambda s: seen.append((rig.log[-1] if rig.log else "-", dict(s.artifacts)))
    )
    stage = make_tool_runner_stage(rig.deps)
    state = batch(make_state, ("c1", "view_file", {}), artifacts={"a": "/p"})
    await stage(state)
    assert seen == [("-", {"a": "/p"})]  # 绑定发生在任何执行与提交之前


@pytest.mark.unit
async def test_the_bind_hook_is_not_called_when_there_is_nothing_to_run(
    make_state: MakeState, tmp_path: Path
) -> None:
    calls: list[AgentState] = []
    rig = Rig(tmp_path)
    stage = make_tool_runner_stage(dataclasses.replace(rig.deps, bind=calls.append))
    assert await stage(with_state(make_state(), phase="tool_runner")) == {}
    assert calls == []


@pytest.mark.unit
async def test_an_artifact_the_tool_itself_produced_is_registered_with_a_handle_when_truncated(
    make_state: MakeState, tmp_path: Path
) -> None:
    result = ToolResult(
        ok=True,
        content="head...tail",
        artifact_id="step_2_bash",
        artifact_path="/store/t/step_2_bash.log",
        is_truncated=True,
    )
    rig = Rig(tmp_path, tools=[Fake("bash", result=result)])
    _, after = await rig.run(batch(make_state, ("c1", "bash", {"command": "make"}), step_count=2))
    assert after.artifacts == {"step_2_bash": "/store/t/step_2_bash.log"}
    assert "artifact://step_2_bash" in tool_messages(after)[0].content


@pytest.mark.unit
async def test_an_untruncated_tool_artifact_is_registered_but_not_advertised(
    make_state: MakeState, tmp_path: Path
) -> None:
    result = ToolResult(
        ok=True, content="all of it", artifact_id="step_2_bash", artifact_path="/store/x.log"
    )
    rig = Rig(tmp_path, tools=[Fake("bash", result=result)])
    _, after = await rig.run(batch(make_state, ("c1", "bash", {"command": "ls"})))
    assert "step_2_bash" in after.artifacts
    assert tool_messages(after)[0].content == "all of it"


@pytest.mark.unit
async def test_a_tool_artifact_id_that_is_already_taken_gets_a_suffix_not_an_overwrite(
    make_state: MakeState, tmp_path: Path
) -> None:
    result = ToolResult(
        ok=True, content="x", artifact_id="step_2_bash", artifact_path="/new.log", is_truncated=True
    )
    rig = Rig(tmp_path, tools=[Fake("bash", result=result)])
    state = batch(
        make_state, ("c1", "bash", {"command": "a"}), artifacts={"step_2_bash": "/old.log"}
    )
    _, after = await rig.run(state)
    assert after.artifacts == {"step_2_bash": "/old.log", "step_2_bash_2": "/new.log"}
    assert "artifact://step_2_bash_2" in tool_messages(after)[0].content


# ==============================================================================
# 预算账本：子级消耗并入 total_tokens，且不被重复扣减
# ==============================================================================


class Spender(Fake):
    """一个会经账本消耗额度的工具：预留、花掉、结算，和 delegate_research 的模式一致。"""

    def __init__(self, ledger: Any, spend: int) -> None:
        super().__init__("view_file", idempotent=True)
        self._ledger, self._spend = ledger, spend

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        reservation, reason = self._ledger.reserve(5_000, "child")
        if reservation is None:
            return ToolResult.failure(f"预算不足：{reason}")
        try:
            return ToolResult(ok=True, content="done")
        finally:
            self._ledger.settle(reservation, self._spend)


@pytest.mark.unit
async def test_child_consumption_is_merged_into_total_tokens_once_per_batch(
    make_state: MakeState, tmp_path: Path
) -> None:
    from agent_runtime.guardrails import BudgetLedger

    ledger = BudgetLedger(max_total_tokens=20_000, min_grant_tokens=1_000)
    rig = Rig(tmp_path, tools=[Spender(ledger, 3_000)], ledger=ledger)
    state = batch(make_state, ("c1", "view_file", {"path": "a"}), total_tokens=10_000)
    update, after = await rig.run(state)
    assert update["total_tokens"] == 13_000 and after.total_tokens == 13_000
    assert ledger.available == 20_000 - 10_000 - 3_000  # 子级的 3000 只算一次

    # 第二次派发（下一批，路径不同以避开重复指纹）：已并入的部分不会再被扣一遍
    second = with_state(
        after,
        phase="tool_runner",
        messages=(
            *after.messages,
            Message(
                role="assistant",
                content="",
                tool_calls=(ToolCall(id="c2", name="view_file", args={"path": "b"}),),
            ),
        ),
    )
    update2, _ = await rig.run(second)
    assert update2["total_tokens"] == 16_000
    assert ledger.available == 20_000 - 10_000 - 6_000
    assert ledger.consumed == 6_000


@pytest.mark.unit
async def test_total_tokens_is_untouched_when_nothing_was_spent(
    make_state: MakeState, tmp_path: Path
) -> None:
    from agent_runtime.guardrails import BudgetLedger

    ledger = BudgetLedger(max_total_tokens=20_000)
    rig = Rig(tmp_path, ledger=ledger)
    update, _ = await rig.run(batch(make_state, ("c1", "view_file", {"path": "a"})))
    assert "total_tokens" not in update


# ==============================================================================
# 回路控制：何时回 planner（replan_needed）与“改动后验证了没有”（verification）
# ==============================================================================

FAIL = ToolResult(ok=False, content="", error="exit 1", exit_code=1)


def interval(n: int) -> LoopLimits:
    return LoopLimits(consecutive_errors_limit=3, identical_fingerprint_limit=3, replan_interval=n)


@pytest.mark.unit
async def test_a_healthy_batch_goes_straight_back_to_the_executor(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, limits=interval(3))
    update, after = await rig.run(batch(make_state, ("c1", "view_file", {"path": "a"})))
    assert update["replan_needed"] is False and update["steps_since_plan"] == 1
    assert next_phase("tool_runner", after) == "budget_guard"


@pytest.mark.unit
async def test_the_planner_is_consulted_again_once_the_interval_is_reached(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, limits=interval(3))
    state = batch(make_state, ("c1", "view_file", {"path": "a"}), steps_since_plan=2)
    _, after = await rig.run(state)
    assert after.replan_needed is True and after.steps_since_plan == 3
    assert next_phase("tool_runner", after) == "planner"


@pytest.mark.unit
async def test_interval_zero_keeps_the_old_replan_after_every_batch(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, limits=interval(0))
    _, after = await rig.run(batch(make_state, ("c1", "view_file", {"path": "a"})))
    assert after.replan_needed is True


@pytest.mark.unit
async def test_consecutive_failures_replan_immediately_whatever_the_interval(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, tools=[Fake("bash", result=FAIL)], limits=interval(99))
    state = batch(make_state, ("c1", "bash", {"command": "pytest"}), consecutive_errors=2)
    _, after = await rig.run(state)
    assert after.consecutive_errors == 3 and after.replan_needed is True


@pytest.mark.unit
async def test_a_blocked_loop_replans_immediately(make_state: MakeState, tmp_path: Path) -> None:
    rig = Rig(tmp_path, limits=interval(99))
    same = compute_fingerprint("view_file", {"path": "a"})
    state = batch(
        make_state,
        ("c1", "view_file", {"path": "a"}),
        fingerprint_history=(same, same),
    )
    _, after = await rig.run(state)
    assert after.replan_needed is True


@pytest.mark.unit
async def test_an_alternating_pattern_is_caught_as_a_loop(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, limits=interval(99))
    a = compute_fingerprint("view_file", {"path": "a"})
    b = compute_fingerprint("view_file", {"path": "b"})
    state = batch(
        make_state, ("c1", "view_file", {"path": "a"}), fingerprint_history=(b, a, b, a, b)
    )
    update, after = await rig.run(state)
    assert after.replan_needed is True
    assert "已拦截" in tool_messages(after)[-1].content
    assert update["steps_since_plan"] == 1


@pytest.mark.unit
async def test_a_human_rejection_replans_immediately(make_state: MakeState, tmp_path: Path) -> None:
    rig = Rig(tmp_path, limits=interval(99))
    _, pending = await suspend(rig, batch(make_state, CURL, permission_level="workspace_write"))
    _, after = await rig.run(approved(pending, approved=False, reason="不允许联网"))
    assert after.replan_needed is True and next_phase("tool_runner", after) == "planner"


@pytest.mark.unit
async def test_verification_follows_edits_and_the_commands_run_after_them(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, limits=interval(99))
    _, after = await rig.run(batch(make_state, ("c1", "bash", {"command": "pytest"})))
    assert after.verification == "none"  # 没改过文件：读代码用的命令不算验证

    _, edited = await rig.run(batch(make_state, ("c1", "write_file", {"path": "a.py"})))
    assert edited.verification == "unchecked"

    _, passed = await rig.run(
        batch(make_state, ("c2", "bash", {"command": "pytest"}), verification="unchecked")
    )
    assert passed.verification == "passed"

    failing = Rig(tmp_path, tools=[Fake("bash", result=FAIL)], limits=interval(99))
    _, failed = await failing.run(
        batch(make_state, ("c3", "bash", {"command": "pytest"}), verification="unchecked")
    )
    assert failed.verification == "failed"


@pytest.mark.unit
async def test_a_new_edit_after_a_failed_check_makes_it_unchecked_again(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, limits=interval(99))
    _, after = await rig.run(
        batch(
            make_state,
            ("c1", "write_file", {"path": "a.py"}),
            ("c2", "bash", {"command": "pytest"}),
            ("c3", "write_file", {"path": "b.py"}),
            verification="failed",
        )
    )
    assert after.verification == "unchecked"  # 按调用顺序：最后一次是改文件


@pytest.mark.unit
async def test_a_failed_edit_or_a_blocked_command_does_not_change_verification(
    make_state: MakeState, tmp_path: Path
) -> None:
    rig = Rig(tmp_path, tools=[Fake("write_file", result=FAIL), Fake("bash")], limits=interval(99))
    _, after = await rig.run(batch(make_state, ("c1", "write_file", {"path": "a.py"})))
    assert after.verification == "none"
