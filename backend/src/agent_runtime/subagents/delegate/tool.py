"""``spawn_subagent``：动态委派。设计文档：docs/06_subagents/02_dynamic_delegation.md §2-3、§5.3。

入参是主模型生成的**提议**，不是授权。派发前一次性完成三道收窄（工具集全有或全无、权限不升级、
深度），之后子级面对的是一个已经定死的世界。返回值恒标 ``untrusted``；拿到结构良好的报告就算成功。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

from agent_runtime.core import AegisTool, ToolResult
from agent_runtime.guardrails import BudgetLedger, ChildBudget
from agent_runtime.subagents.delegate.render import render_report
from agent_runtime.subagents.delegate.runner import SubagentRunner
from tools.core import ToolRegistry

_MAX_ROLE = 120
_MAX_GOAL = 1200
_DEADLINE_MARGIN_SEC = 15.0


class SpawnSubagentTool(AegisTool):
    name = "spawn_subagent"
    description = (
        "临时组建一个受限的子劳动力，去完成一个需要阅读大量材料、但只需回流简短结论的子任务"
        "（例如通读十几个文件找出某个调用链）。你必须明确列出分配给它的工具；它的权限不会高于你，"
        "遇到需要审批的操作会直接失败。回流的是带引用的结论，可能失真，请用本地证据交叉核对。"
        "每个任务可派发的次数有限，请把子任务想清楚再派。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "子劳动力的角色代号，如 code_reader"},
            "goal": {"type": "string", "description": "它要完成的具体目标"},
            "assigned_tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "分配给它的工具名，必须是你当前拥有的工具的子集",
            },
            "max_steps": {"type": "integer", "description": "可选，步数上限（受配置上限约束）"},
            "token_budget": {
                "type": "integer",
                "description": "可选，Token 预算（实际授予量受余额收窄）",
            },
        },
        "required": ["role", "goal", "assigned_tools"],
    }
    #: 接口本身可信；每次返回时在结果上覆盖为 untrusted
    trust = "trusted"
    idempotent = False

    def __init__(
        self,
        runner_factory: Callable[[ToolRegistry], SubagentRunner],
        parent_tools: Callable[[], Mapping[str, AegisTool]],
        ledger: BudgetLedger,
        *,
        enabled: bool,
        depth: int,
        max_depth: int,
        denied_tools: Collection[str],
        max_assigned_tools: int,
        max_steps: int,
        max_total_tokens: int,
        max_wall_time_sec: float,
        max_report_chars: int,
    ) -> None:
        self._factory = runner_factory
        self._parent_tools = parent_tools
        self._ledger = ledger
        self._enabled = enabled
        self._depth = depth
        self._max_depth = max_depth
        self._denied = set(denied_tools) | {self.name}
        self._max_assigned = max_assigned_tools
        self._max_steps = max_steps
        self._max_tokens = max_total_tokens
        self._max_wall = max_wall_time_sec
        self._max_report = max_report_chars
        self.timeout_sec = max_wall_time_sec + _DEADLINE_MARGIN_SEC

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        if not self._enabled:
            return ToolResult.failure("动态委派未启用，请自行完成")
        if self._depth + 1 > self._max_depth:
            return ToolResult.failure("已达到委派深度上限，不能再委派；请自行完成")
        role = args.get("role")
        goal = args.get("goal")
        if not isinstance(role, str) or not role.strip():
            return ToolResult.failure("缺少参数 role（角色代号）")
        if not isinstance(goal, str) or not goal.strip():
            return ToolResult.failure("缺少参数 goal（子任务目标）")
        narrowed = self._narrow(args.get("assigned_tools"))
        if isinstance(narrowed, str):
            return ToolResult.failure(narrowed)
        reservation, reason = self._ledger.reserve(self._requested_tokens(args), f"subagent:{role}")
        if reservation is None:
            return ToolResult.failure(f"预算不足，无法派发：{reason}")
        budget = ChildBudget(reservation.amount, self._max_wall)
        steps = self._steps_from(args)
        runner = self._factory(ToolRegistry(narrowed, allow_untrusted=True))
        try:
            report = await runner.run(
                role.strip()[:_MAX_ROLE], goal.strip()[:_MAX_GOAL], budget, steps
            )
        finally:
            self._ledger.settle(
                reservation, budget.used
            )  # 所有退出路径：成功、失败、超时被取消、异常
        return ToolResult(
            ok=True,  # 拿到结构良好的报告就算成功：子级如实报告“做不到”是有效结论
            content=render_report(
                report, role=role.strip()[:_MAX_ROLE], max_chars=self._max_report
            ),
            meta={
                "status": report.status,
                "steps": report.steps_used,
                "tool_calls": report.tool_calls,
                "tokens": budget.used,
            },
            trust="untrusted",
        )

    def _narrow(self, requested: object) -> list[AegisTool] | str:
        """工具集收窄：全有或全无，不做静默收窄。"""
        if not isinstance(requested, list) or not requested:
            return "assigned_tools 不能为空：不提供默认授权，不给就是没有"
        names = [n for n in requested if isinstance(n, str)]
        if len(names) != len(requested):
            return "assigned_tools 必须是工具名字符串列表"
        unique = list(dict.fromkeys(names))
        parent = self._parent_tools()
        grantable = sorted(n for n in parent if n not in self._denied)
        bad = [n for n in unique if n not in parent or n in self._denied]
        if bad:
            return f"这些工具不能分配：{bad}。当前可授权的工具：{grantable}"
        if len(unique) > self._max_assigned:
            return f"分配的工具超过上限 {self._max_assigned} 个。当前可授权的工具：{grantable}"
        return [parent[n] for n in unique]

    def _requested_tokens(self, args: Mapping[str, Any]) -> int:
        asked = args.get("token_budget")
        if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0:
            return min(asked, self._max_tokens)
        return self._max_tokens

    def _steps_from(self, args: Mapping[str, Any]) -> int:
        asked = args.get("max_steps")
        if isinstance(asked, int) and not isinstance(asked, bool) and asked > 0:
            return min(asked, self._max_steps)
        return self._max_steps
