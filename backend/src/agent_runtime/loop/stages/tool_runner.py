"""tool_runner 阶段：权限闸门、派发前标记、并发派发、观察值治理、指纹与连续错误护栏。

设计文档：docs/02_runtime/02_main_loop.md §5、04_approval.md、05_security/01_permission_tiers.md。

**执行顺序固定，不可调换：** 权限判定（越级且无决策则挂起，此前无任何副作用）→ 指纹登记与
死循环判定 → 派发前标记提交 → 并发派发 → 观察值治理与配对消息 → 连续错误与重规划通知。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from agent_runtime.context import ObservationGovernor
from agent_runtime.core import (
    AgentState,
    ApprovalRequired,
    Message,
    PendingApproval,
    ToolCall,
    Verification,
    apply_update,
)
from agent_runtime.guardrails import (
    BudgetLedger,
    LoopLimits,
    PermissionDecision,
    PermissionPolicy,
    action_signature,
    build_replan_notice,
    check_permission,
    is_alternating,
    is_fingerprint_loop,
    register_fingerprints,
    update_consecutive_errors,
)
from agent_runtime.guardrails.permissions import LEVEL_ORDER
from agent_runtime.loop.runtime import Stage
from agent_runtime.loop.stages.common import Emit
from agent_runtime.loop.stages.executor import RAW_ARGS_KEY
from tools.core import ToolDispatcher

LOOP_BLOCK_NOTICE: Final = "已拦截：相同工具与参数连续重复，请更换策略后重试。"
MALFORMED_NOTICE: Final = (
    "未执行：该调用的参数无法使用（不是合法的 JSON，或输出被长度上限截断）。"
    "请重新发起调用，并保持参数简短。"
)
REJECTED_PREFIX: Final = "已被人工审核拒绝"
REJECTED_SUFFIX: Final = "请改用合规方案（如仅生成本地补丁而不推送远端）"
DEFAULT_REJECT_REASON: Final = "用户未批准该操作"
_RELATED_LIMIT: Final = 4

#: (工具名, 信任级, 裁剪后的文本) -> 放进上下文的文本。信封协议见 05_security/03_trust_and_injection
Envelope = Callable[[str, str, str], str]


@dataclass(frozen=True, slots=True)
class ApprovalHooks:
    """审批协议的两个纯函数。``loop`` 与 ``approval`` 是同层模块、不能互相导入，所以由装配方注入
    （真实实现是 ``agent_runtime.approval`` 的 ``compute_approval_id`` 与 ``check_binding``）。"""

    approval_id: Callable[[str, int, Iterable[str]], str]
    check_binding: Callable[[AgentState, Iterable[str]], None]


@dataclass(frozen=True, slots=True)
class ToolRunnerDeps:
    dispatcher: ToolDispatcher
    policy: PermissionPolicy
    limits: LoopLimits
    governor: ObservationGovernor
    approval: ApprovalHooks
    #: 阶段中途的那一次提交（派发前标记）。检查点存储的 ``commit`` 满足它
    commit: Callable[[AgentState], Awaitable[object]]
    #: 会话级“永久放行”签名集合（可变引用，由任务注册表持有，跨续跑存活，不入检查点）
    allowlist: set[str] = field(default_factory=set[str])
    emit: Emit | None = None
    #: 子智能体的预算账本：派发前推送父任务用量，派发后把子级的增量消耗并入 ``total_tokens``
    ledger: BudgetLedger | None = None
    dispatch_marker: bool = True
    envelope: Envelope | None = None
    #: 有调用要处理时，先把状态推给工具侧的任务绑定对象（如 ``read_artifact`` 能看见的产物表）
    bind: Callable[[AgentState], None] | None = None


_HANDLE = re.compile(r"artifact://([A-Za-z0-9_.-]+)")


def _artifact_of(content: str) -> str:
    """观察值末尾登记的离线副本句柄；没有则为空。"""
    found = _HANDLE.findall(content)
    return found[-1] if found else ""


@dataclass(frozen=True, slots=True)
class _Synthetic:
    text: str
    counts_as_failure: bool


def make_tool_runner_stage(deps: ToolRunnerDeps) -> Stage:
    async def tool_runner(state: AgentState) -> Mapping[str, object]:
        last = state.messages[-1] if state.messages else None
        if last is None or last.role != "assistant" or not last.tool_calls:
            return {}
        calls = last.tool_calls
        if deps.bind is not None:
            deps.bind(state)

        # 1. 权限闸门。挂起（抛出）之前不得有任何外部副作用，也不提交
        rejected, consumed = await _gate(deps, state, calls)

        # 2. 指纹登记与死循环判定
        history = register_fingerprints(
            state.fingerprint_history,
            [(c.name, c.args) for c in calls],
            deps.limits.window,
        )
        loop = is_fingerprint_loop(
            history, deps.limits.identical_fingerprint_limit
        ) or is_alternating(history, deps.limits.alternation_cycles)
        if loop:
            await _emit(deps, "guard.loop_blocked", {"call_ids": [c.id for c in calls]})

        # 3. 决定哪些调用真的派发
        synthetic = _synthesize(calls, rejected, loop)
        dispatchable = [c for c in calls if c.id not in synthetic]

        # 4. 派发前标记：崩溃后据此识别“可能已部分执行”，把“至少一次”收敛成“至多一次”
        if dispatchable and deps.dispatch_marker:
            marked = apply_update(consumed, {"dispatch_started": tuple(c.id for c in dispatchable)})
            await deps.commit(marked)

        # 5. 并发派发（账本：先推送父级用量，派发后只取本批的增量）
        consumed_before = 0
        if deps.ledger is not None:
            deps.ledger.sync_parent_usage(state.total_tokens)
            consumed_before = deps.ledger.consumed
        dispatched = await deps.dispatcher.dispatch([(c.id, c.name, c.args) for c in dispatchable])
        child_tokens = 0
        if deps.ledger is not None:
            child_tokens = deps.ledger.consumed - consumed_before
            deps.ledger.mark_merged(child_tokens)
        by_id = {item.tool_call_id: item for item in dispatched}

        # 6. 观察值治理与配对消息（每个调用都有，顺序与调用一致）
        artifacts = dict(state.artifacts)
        messages: list[Message] = []
        failures = 0
        for call in calls:
            pre = synthetic.get(call.id)
            if pre is not None:
                content, failed = pre.text, pre.counts_as_failure
            else:
                item = by_id[call.id]
                result = item.result
                governed = await deps.governor.govern(
                    result.content or result.error or "",
                    task_id=state.task_id,
                    step=state.step_count,
                    tool_name=call.name,
                    taken=artifacts.keys(),
                )
                if governed.artifact_id and governed.artifact_path:
                    artifacts[governed.artifact_id] = governed.artifact_path
                content, failed = governed.summary, result.failed
                if result.artifact_id and result.artifact_path:
                    # 工具自带的全量产物（如 sidecar 的命令日志）登记进同一张表
                    own = _unique_id(result.artifact_id, artifacts)
                    artifacts[own] = result.artifact_path
                    if result.is_truncated:
                        content += f"\n[完整输出: artifact://{own}]"
                if item.trust == "untrusted" and deps.envelope is not None:
                    # 信封在裁剪之后包：警示语不会被截掉
                    content = deps.envelope(call.name, item.trust, content)
            failures += failed
            await _emit(
                deps,
                "tool.observed",
                {"call_id": call.id, "ok": not failed, "artifact_id": _artifact_of(content)},
            )
            messages.append(
                Message(role="tool", content=content, tool_call_id=call.id, name=call.name)
            )

        # 7. 连续错误与重规划通知（作为 user 消息追加在工具消息之后：system 只能在最前面）
        errors = update_consecutive_errors(state.consecutive_errors, failures)
        if errors >= deps.limits.consecutive_errors_limit or loop:
            notice = build_replan_notice(errors, loop, deps.limits.consecutive_errors_limit)
            messages.append(Message(role="user", content=notice))
            await _emit(deps, "guard.replan_notice", {"consecutive_errors": errors, "loop": loop})

        steps = state.steps_since_plan + 1
        replan = (
            bool(loop)
            or bool(rejected)
            or errors >= deps.limits.consecutive_errors_limit
            or steps >= deps.limits.replan_interval  # 0：每个批次都回 planner
        )
        verification = _verification_after(
            state.verification, calls, {c.id: by_id[c.id].result.failed for c in dispatchable}
        )

        usage: dict[str, object] = (
            {"total_tokens": state.total_tokens + child_tokens} if child_tokens else {}
        )
        return {
            **usage,
            "messages": messages,
            "consecutive_errors": errors,
            "fingerprint_history": history,
            "steps_since_plan": steps,
            "replan_needed": replan,
            "verification": verification,
            "artifacts": artifacts,
            "dispatch_started": (),
            "pending_approval": None,
            "approval_decision": None,
        }

    return tool_runner


_EDIT_TOOLS: Final = frozenset({"write_file", "edit_file"})
_COMMAND_TOOL: Final = "bash"


def _verification_after(
    current: Verification, calls: Iterable[ToolCall], failed: Mapping[str, bool]
) -> Verification:
    """按调用顺序更新“改动后验证了没有”：改文件成功 → 未验证；之后的命令按成败定 passed / failed。

    没有改过文件时命令不影响它（读代码用的命令不算验证）；被拦截或拒绝的调用没有执行，不计。
    """
    state: Verification = current
    for call in calls:
        if call.id not in failed:
            continue
        if call.name in _EDIT_TOOLS and not failed[call.id]:
            state = "unchecked"
        elif call.name == _COMMAND_TOOL and state != "none":
            state = "failed" if failed[call.id] else "passed"
    return state


def _unique_id(base: str, taken: Mapping[str, str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


async def _gate(
    deps: ToolRunnerDeps, state: AgentState, calls: tuple[ToolCall, ...]
) -> tuple[dict[str, str], AgentState]:
    """返回 (被人工拒绝的 call_id → 原因, 已消费审批字段的状态)。"""
    decisions: dict[str, PermissionDecision] = {}
    escalated: list[ToolCall] = []
    for call in calls:
        # 不存在的工具与参数无法使用的调用都不会被执行，没必要打扰人
        if not deps.dispatcher.knows(call.name) or _is_unusable(call):
            continue
        decision = check_permission(deps.policy, state.permission_level, call.name, call.args)
        if decision.allowed or action_signature(call.name, call.args) in deps.allowlist:
            continue
        decisions[call.id] = decision
        escalated.append(call)

    rejected: dict[str, str] = {}
    if escalated:
        if state.approval_decision is None:
            raise ApprovalRequired(_request(deps, state, escalated, decisions))
        ids = [c.id for c in escalated]
        deps.approval.check_binding(state, ids)  # 决策必须对应这一批，否则报错而不是套用
        verdict = state.approval_decision
        await _emit(
            deps,
            "guard.approval_decided",
            {
                "approval_id": verdict.approval_id,
                "approved": verdict.approved,
                "scope": verdict.scope,
                "reason": verdict.reason,
                "call_ids": ids,
            },
        )
        if not verdict.approved:
            reason = verdict.reason or DEFAULT_REJECT_REASON
            rejected = dict.fromkeys(ids, reason)
        elif verdict.scope == "always":
            deps.allowlist.update(action_signature(c.name, c.args) for c in escalated)

    # 没有（或已无）需要审批的调用时，残留的审批字段是过期的：清掉，不授予任何东西
    cleared = apply_update(state, {"pending_approval": None, "approval_decision": None})
    return rejected, cleared


def _request(
    deps: ToolRunnerDeps,
    state: AgentState,
    escalated: list[ToolCall],
    decisions: Mapping[str, PermissionDecision],
) -> PendingApproval:
    ordered = [decisions[c.id] for c in escalated]
    first = ordered[0]
    highest = max((d.required_level for d in ordered), key=lambda level: LEVEL_ORDER[level])
    ids = tuple(c.id for c in escalated)
    return PendingApproval(
        approval_id=deps.approval.approval_id(state.task_id, state.step_count, ids),
        call_ids=ids,
        required_level=highest,  # type: ignore[arg-type]
        current_level=state.permission_level,
        action_type=first.action_type,
        command=first.action_summary,
        reason=first.reason,
        escalation_count=len(ordered),
        related_actions=tuple(d.action_summary for d in ordered[1 : 1 + _RELATED_LIMIT]),
    )


def _is_unusable(call: ToolCall) -> bool:
    raw = call.args.get(RAW_ARGS_KEY)
    return set(call.args) == {RAW_ARGS_KEY} and isinstance(raw, str)


def _synthesize(
    calls: tuple[ToolCall, ...], rejected: Mapping[str, str], loop: bool
) -> dict[str, _Synthetic]:
    out: dict[str, _Synthetic] = {}
    for call in calls:
        if loop:
            out[call.id] = _Synthetic(LOOP_BLOCK_NOTICE, True)
        elif _is_unusable(call):
            out[call.id] = _Synthetic(MALFORMED_NOTICE, True)
        elif call.id in rejected:
            text = f"{REJECTED_PREFIX}：{rejected[call.id]}，{REJECTED_SUFFIX}"
            out[call.id] = _Synthetic(text, False)  # 人工拒绝不是工具失败，不累计
    return out


async def _emit(deps: ToolRunnerDeps, name: str, payload: Mapping[str, object]) -> None:
    if deps.emit is None:
        return
    try:
        await deps.emit(name, payload)
    except Exception:
        from loguru import logger

        logger.exception("事件发送失败（已忽略）")
