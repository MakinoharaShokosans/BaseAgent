"""工具包装层：把一批工具调用变成与调用一一对应的 ``tool`` 消息。

设计文档：docs/02_runtime/06_tool_execution.md。执行顺序固定：解析代理 → 权限闸门（越级无决策
则挂起，此前无任何副作用）→ 指纹登记与拦截 → 派发（只读并发、其余串行）→ 后处理（裁剪、信封、
产物、验证）→ 连续错误通知。它不调用模型。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from loguru import logger

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
    LoopLimits,
    PermissionDecision,
    PermissionPolicy,
    action_signature,
    build_error_notice,
    check_permission,
    is_alternating,
    is_fingerprint_loop,
    register_fingerprints,
    update_consecutive_errors,
)
from agent_runtime.guardrails.permissions import LEVEL_ORDER
from agent_runtime.loop.proxy import McpProxy, Resolved
from agent_runtime.loop.scratch import SCRATCH_TOOLS, Scratch, apply_scratch
from tools.core import ToolDispatcher

#: 参数无法使用的调用的标记：``args`` 只含这一个键，值是模型给出的原文。
#: 网关再次发给模型时原样送回（见 llm/wire.py），包装层据此不执行。
RAW_ARGS_KEY: Final = "_raw"

LOOP_BLOCK_NOTICE: Final = "该调用与最近的调用重复，已拦截。请换一种做法，或说明受阻原因并结束。"
MALFORMED_NOTICE: Final = (
    "未执行：该调用的参数无法使用（不是合法的 JSON，或输出被长度上限截断）。"
    "请重新发起调用，并保持参数简短。"
)
REJECTED_TEMPLATE: Final = (
    "已被人工审核拒绝：{reason}。不要换着花样绕过这个操作，也不要编造缺失的信息；"
    "改用合规方案，或说明受阻原因并结束。"
)
DEFAULT_REJECT_REASON: Final = "用户未批准该操作"
_RELATED_LIMIT: Final = 4

#: (工具名, 信任级, 裁剪后的文本) -> 放进上下文的文本。信封协议见 05_security/03_trust_and_injection
Envelope = Callable[[str, str, str], str]
Emit = Callable[[str, Mapping[str, object]], Awaitable[None]]


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
    #: 会话级“永久放行”签名集合（可变引用，由任务注册表持有，跨续跑存活，不入检查点）
    allowlist: set[str] = field(default_factory=set[str])
    emit: Emit | None = None
    envelope: Envelope | None = None
    #: 有调用要处理时，先把状态推给工具侧的任务绑定对象（如 ``read_artifact`` 能看见的产物表）
    bind: Callable[[AgentState], None] | None = None
    #: ``mcp_call`` 代理；没有延迟工具时为 None
    proxy: McpProxy | None = None
    #: 参数校验：（状态，调用）→ 失败说明；返回 None 表示放行。在代理解析之后、权限闸门之前
    guards: tuple[Callable[[AgentState, ToolCall], str | None], ...] = ()


_HANDLE = re.compile(r"artifact://([A-Za-z0-9_.-]+)")


def _artifact_of(content: str) -> str:
    found = _HANDLE.findall(content)
    return found[-1] if found else ""


@dataclass(frozen=True, slots=True)
class _Synthetic:
    text: str
    counts_as_failure: bool


@dataclass(frozen=True, slots=True)
class _Call:
    """一次调用及其解析后的目标。"""

    call: ToolCall
    target: Resolved

    @property
    def id(self) -> str:
        return self.call.id


def turn_index(state: AgentState) -> int:
    """当前回合序号：助手消息的条数（含正在处理的这一条）。"""
    return sum(1 for m in state.messages if m.role == "assistant")


def make_tool_runner(
    deps: ToolRunnerDeps,
) -> Callable[[AgentState, tuple[ToolCall, ...]], Awaitable[Mapping[str, object]]]:
    async def run(state: AgentState, calls: tuple[ToolCall, ...]) -> Mapping[str, object]:
        if deps.bind is not None:
            deps.bind(state)
        turn = turn_index(state)

        # 1. 校验：解析代理，标出不会执行的调用
        resolved = [_Call(c, _resolve(deps, state, c)) for c in calls]

        # 2. 权限闸门。挂起（抛出）之前不得有任何外部副作用，也不提交
        rejected, consumed = await _gate(deps, state, resolved)

        # 3. 指纹登记与拦截
        history = register_fingerprints(
            state.fingerprint_history,
            [(r.target.name, r.target.args) for r in resolved],
            deps.limits.window,
        )
        loop = is_fingerprint_loop(
            history, deps.limits.identical_fingerprint_limit
        ) or is_alternating(history, deps.limits.alternation_cycles)
        if loop:
            await _emit(deps, "guard.loop_blocked", {"call_ids": [r.id for r in resolved]})

        synthetic = _synthesize(resolved, rejected, loop)
        scratch = Scratch(state.plan, state.facts, state.failed_attempts)

        # 4. 派发：草稿工具就地处理；只读并发，其余按调用顺序串行
        results = await _dispatch(deps, resolved, synthetic)

        # 5. 后处理
        artifacts = dict(state.artifacts)
        messages: list[Message] = []
        failures = 0
        plan_changed = False
        for r in resolved:
            call = r.call
            pre = synthetic.get(r.id)
            detail: dict[str, object] = {"call_id": r.id}
            if pre is not None:
                content, failed = pre.text, pre.counts_as_failure
            elif call.name in SCRATCH_TOOLS:
                outcome = apply_scratch(scratch, call.name, call.args)
                scratch, content, failed = outcome.scratch, outcome.text, not outcome.ok
                plan_changed = plan_changed or outcome.plan_changed
            else:
                item = results[r.id]
                result = item.result
                governed = await deps.governor.govern(
                    result.content or result.error or "",
                    task_id=state.task_id,
                    step=turn,
                    tool_name=r.target.name,
                    taken=artifacts.keys(),
                )
                artifact_id = result.artifact_id
                if governed.artifact_id and governed.artifact_path:
                    artifacts[governed.artifact_id] = governed.artifact_path
                    artifact_id = artifact_id or governed.artifact_id
                content, failed = governed.summary, result.failed
                if result.artifact_id and result.artifact_path:
                    own = _unique_id(result.artifact_id, artifacts)
                    artifacts[own] = result.artifact_path
                    artifact_id = own
                    if result.is_truncated:
                        content += f"\n[完整输出: artifact://{own}]"
                if item.trust == "untrusted" and not failed and deps.envelope is not None:
                    content = deps.envelope(r.target.name, item.trust, content)  # 裁剪之后再包
                detail.update(
                    artifact_id=artifact_id or _artifact_of(content) or "",
                    duration_ms=result.duration_ms,
                    exit_code=result.exit_code,
                    truncated=bool(result.is_truncated or governed.truncated),
                    bytes=len(result.content.encode("utf-8")),
                )
                changed = result.meta.get("changed_files")
                if isinstance(changed, list):
                    detail["changed_files"] = changed
                if result.meta.get("timed_out") is True:
                    detail["timed_out"] = True
            failures += failed
            detail["ok"] = not failed
            detail.setdefault("artifact_id", _artifact_of(content) or "")
            await _emit(deps, "tool.observed", detail)
            messages.append(
                Message(role="tool", content=content, tool_call_id=r.id, name=call.name)
            )

        # 6. 连续错误通知（user 角色，追加在工具消息之后：system 只能在最前面）
        errors = update_consecutive_errors(state.consecutive_errors, failures)
        if errors >= deps.limits.consecutive_errors_limit:
            messages.append(Message(role="user", content=build_error_notice(errors)))
            await _emit(deps, "guard.error_notice", {"consecutive_errors": errors})

        verification = _verification_after(
            state.verification,
            [(r.target.name, r.id) for r in resolved],
            {r.id: results[r.id].result.failed for r in resolved if r.id in results},
        )
        update: dict[str, object] = {
            "messages": messages,
            "consecutive_errors": errors,
            "fingerprint_history": history,
            "consecutive_blocks": state.consecutive_blocks + 1 if loop else 0,
            "verification": verification,
            "artifacts": artifacts,
            "pending_approval": None,
            "approval_decision": None,
        }
        if scratch != Scratch(state.plan, state.facts, state.failed_attempts):
            update.update(
                plan=scratch.plan, facts=scratch.facts, failed_attempts=scratch.failed_attempts
            )
        if plan_changed:
            await _emit(deps, "plan.changed", {})
        _ = consumed  # 审批字段由 update 清掉；consumed 只用于闸门内的绑定校验
        return update

    return run


def _resolve(deps: ToolRunnerDeps, state: AgentState, call: ToolCall) -> Resolved:
    if _is_unusable(call):
        return Resolved(call.name, call.args, MALFORMED_NOTICE)
    resolved = (
        Resolved(call.name, call.args) if deps.proxy is None else deps.proxy.resolve(state, call)
    )
    if resolved.error:
        return resolved
    target = ToolCall(id=call.id, name=resolved.name, args=resolved.args)
    for guard in deps.guards:
        if (problem := guard(state, target)) is not None:
            return Resolved(resolved.name, resolved.args, problem)
    return resolved


async def _dispatch(
    deps: ToolRunnerDeps, resolved: list[_Call], synthetic: Mapping[str, _Synthetic]
):  # type: ignore[no-untyped-def]
    """返回 call_id -> 结果。连续的只读调用并发，其余逐个串行，组间保持调用顺序。"""
    from tools.core.dispatcher import DispatchedResult

    out: dict[str, DispatchedResult] = {}
    todo = [r for r in resolved if r.id not in synthetic and r.call.name not in SCRATCH_TOOLS]
    group: list[_Call] = []

    async def flush() -> None:
        if not group:
            return
        batch = [(r.id, r.target.name, r.target.args) for r in group]
        for item in await deps.dispatcher.dispatch(batch):
            out[item.tool_call_id] = item
        group.clear()

    for r in todo:
        if r.target.name in deps.policy.read_only_tools:
            group.append(r)
        else:
            await flush()
            group.append(r)
            await flush()
    await flush()
    return out


_EDIT_TOOLS: Final = frozenset({"write_file", "edit_file"})
_COMMAND_TOOL: Final = "bash"


def _verification_after(
    current: Verification, calls: Iterable[tuple[str, str]], failed: Mapping[str, bool]
) -> Verification:
    """按调用顺序更新“改动后验证了没有”：改文件成功 → 未验证；之后的命令按成败定 passed / failed。

    没有改过文件时命令不影响它；被拦截或拒绝的调用没有执行，不计。
    """
    state: Verification = current
    for name, call_id in calls:
        if call_id not in failed:
            continue
        if name in _EDIT_TOOLS and not failed[call_id]:
            state = "unchecked"
        elif name == _COMMAND_TOOL and state != "none":
            state = "failed" if failed[call_id] else "passed"
    return state


def _unique_id(base: str, taken: Mapping[str, str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


async def _gate(
    deps: ToolRunnerDeps, state: AgentState, calls: list[_Call]
) -> tuple[dict[str, str], AgentState]:
    """返回 (被人工拒绝的 call_id → 原因, 已消费审批字段的状态)。权限判定按**目标工具**。"""
    decisions: dict[str, PermissionDecision] = {}
    escalated: list[_Call] = []
    for r in calls:
        if r.target.error or not deps.dispatcher.knows(r.target.name):
            continue  # 不会被执行的调用不值得打扰人
        decision = check_permission(
            deps.policy, state.permission_level, r.target.name, r.target.args
        )
        if decision.allowed or action_signature(r.target.name, r.target.args) in deps.allowlist:
            continue
        decisions[r.id] = decision
        escalated.append(r)

    rejected: dict[str, str] = {}
    if escalated:
        if state.approval_decision is None:
            raise ApprovalRequired(_request(deps, state, escalated, decisions))
        ids = [r.id for r in escalated]
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
            rejected = dict.fromkeys(ids, verdict.reason or DEFAULT_REJECT_REASON)
        elif verdict.scope == "always":
            deps.allowlist.update(action_signature(r.target.name, r.target.args) for r in escalated)

    cleared = apply_update(state, {"pending_approval": None, "approval_decision": None})
    return rejected, cleared


def _request(
    deps: ToolRunnerDeps,
    state: AgentState,
    escalated: list[_Call],
    decisions: Mapping[str, PermissionDecision],
) -> PendingApproval:
    ordered = [decisions[r.id] for r in escalated]
    first = ordered[0]
    highest = max((d.required_level for d in ordered), key=lambda level: LEVEL_ORDER[level])
    ids = tuple(r.id for r in escalated)
    return PendingApproval(
        approval_id=deps.approval.approval_id(state.task_id, len(state.messages), ids),
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
    calls: list[_Call], rejected: Mapping[str, str], loop: bool
) -> dict[str, _Synthetic]:
    out: dict[str, _Synthetic] = {}
    for r in calls:
        if loop:
            out[r.id] = _Synthetic(LOOP_BLOCK_NOTICE, True)
        elif r.target.error:
            out[r.id] = _Synthetic(r.target.error, True)  # 模型的错，计入连续错误
        elif r.id in rejected:
            out[r.id] = _Synthetic(REJECTED_TEMPLATE.format(reason=rejected[r.id]), False)
    return out


async def _emit(deps: ToolRunnerDeps, name: str, payload: Mapping[str, object]) -> None:
    if deps.emit is None:
        return
    try:
        await deps.emit(name, payload)
    except Exception:
        logger.exception("事件发送失败（已忽略）")
