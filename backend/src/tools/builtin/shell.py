"""bash 工具：``bash_shell`` sidecar 的薄客户端。设计文档：04_tools/03_shell_tool.md §1。

沙箱判断全在 sidecar；这里只组装请求、转译结果，并把“策略拒绝”和“依赖故障”分开。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from agent_runtime.core import AegisTool, ToolResult
from tools.core import DependencyUnavailableError, SidecarClient

_EXECUTE: Final = "/api/v1/shell/execute"
_ACCEPTED: Final = (200, 403, 422)
#: 工具自身的硬超时要比最长的命令更久，否则派发器会抢在 sidecar 之前把命令判成超时
_DEADLINE_MARGIN_SEC: Final = 15.0
_NO_OUTPUT: Final = "（无输出）"
_QUEUED_TEXT: Final = "当前并发配额已满，已进入等待队列，请稍后重试或减少并行命令"


class ShellResponse(BaseModel):
    """sidecar 的响应契约；多余字段直接报错（契约漂移要早暴露）。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["SUCCESS", "FAILED", "TIMEOUT", "KILLED", "QUEUED"]
    exit_code: int | None = None
    distilled_stdout: str = ""
    distilled_stderr: str = ""
    is_truncated: bool = False
    artifact_path: str | None = None
    execution_time_ms: int = 0
    timed_out: bool = False
    queue_wait_sec: float | None = None
    estimated_wait_sec: float | None = None


class BashTool(AegisTool):
    name = "bash"
    description = (
        "在工作区根目录执行一条 Shell 命令，返回蒸馏后的输出与退出码。"
        "命令必须非交互（没有标准输入，分页器会卡住，如 git 请用 --no-pager）；"
        "输出很长时只返回头尾与关键行，全文可用 read_artifact 取回；"
        "不要启动常驻进程；被安全策略拒绝后不要换个写法重试，应改用合规方案或如实报告。"
    )
    parameters: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 Shell 命令"},
            "timeout_sec": {"type": "number", "description": "超时秒数，缺省用默认值"},
        },
        "required": ["command"],
    }
    idempotent = False

    def __init__(
        self,
        client: SidecarClient,
        *,
        workspace_id: str,
        workspace_root: str,
        task_id: str,
        default_timeout_sec: float,
        max_timeout_sec: float,
    ) -> None:
        self._client = client
        self._workspace_id = workspace_id
        self._workspace_root = workspace_root
        self._task_id = task_id
        self._default = default_timeout_sec
        self._max = max_timeout_sec
        self.timeout_sec = max_timeout_sec + _DEADLINE_MARGIN_SEC
        #: 只用于 sidecar 的日志命名，与编排层的步号无关
        self._steps = itertools.count(1)

    async def invoke(self, args: Mapping[str, Any]) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.failure("缺少参数 command（非空字符串）")
        timeout = self._timeout_from(args.get("timeout_sec"))
        payload: dict[str, Any] = {
            "workspace_id": self._workspace_id,
            "workspace_root": self._workspace_root,
            "task_id": self._task_id,
            "step_id": next(self._steps),
            "command": command,
        }
        if timeout is not None:
            payload["timeout_sec"] = timeout
        try:
            reply = await self._client.post_json(
                _EXECUTE,
                payload,
                accepted=_ACCEPTED,
                timeout_sec=(timeout or self._default) + _DEADLINE_MARGIN_SEC,
            )
        except DependencyUnavailableError as exc:
            return ToolResult.failure(
                f"sidecar 不可用：{exc}", meta={"dependency_unavailable": True}
            )
        if reply.status != 200:
            return _rejection(reply.status, reply.body)
        try:
            response = ShellResponse.model_validate(reply.body)
        except ValidationError:
            return ToolResult.failure("sidecar 返回了无法识别的响应，已丢弃")
        return _translate(response, timeout or self._default)

    def _timeout_from(self, value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            return None  # 非法值回退默认，不报错：模型多半只是写错了类型
        return min(value, self._max)


def _translate(r: ShellResponse, timeout: float) -> ToolResult:
    meta: dict[str, Any] = {"status": r.status, "execution_time_ms": r.execution_time_ms}
    if r.status == "QUEUED":
        text = _QUEUED_TEXT
        if r.estimated_wait_sec is not None:
            text += f"（预计等待约 {r.estimated_wait_sec:g} 秒）"
        return ToolResult(ok=True, content=text, meta={**meta, "queued": True})

    content = _output(r)
    if r.timed_out or r.status == "TIMEOUT":
        content += f"\n[命令执行超时（超过 {timeout:g} 秒），已被终止]"
        ok = False
    elif r.exit_code is not None:
        ok = r.exit_code == 0
    else:
        ok = r.status == "SUCCESS"
    path = r.artifact_path
    return ToolResult(
        ok=ok,
        content=content,
        exit_code=r.exit_code,
        artifact_id=Path(path).stem if path else None,
        artifact_path=path,
        is_truncated=r.is_truncated,
        duration_ms=r.execution_time_ms,
        meta=meta,
    )


def _output(r: ShellResponse) -> str:
    if r.distilled_stdout and r.distilled_stderr:
        return f"{r.distilled_stdout}\n[stderr]\n{r.distilled_stderr}"
    if r.distilled_stderr:
        return f"[stderr]\n{r.distilled_stderr}"
    return r.distilled_stdout or _NO_OUTPUT


def _rejection(status: int, body: Mapping[str, Any]) -> ToolResult:
    """403 是审计拒绝，422 是写入目标越界或工作区非法：原因必须到达模型。"""
    rule, reason = _rule_and_reason(body)
    head = "命令被安全策略拒绝" if status == 403 else "命令被沙箱拒绝（写入目标越界或工作区非法）"
    details = [f"规则 {rule}"] if rule else []
    if reason:
        details.append(reason)
    message = head + ("：" + "；".join(details) if details else "")
    meta: dict[str, Any] = {"policy_rejected": True, "status": status}
    if rule:
        meta["rule"] = rule
    return ToolResult.failure(message, meta=meta)


def _rule_and_reason(body: Mapping[str, Any]) -> tuple[str | None, str | None]:
    node: Any = body.get("detail", body)
    if isinstance(node, list) and node:
        node = node[0]  # type: ignore[reportUnknownVariableType]
    if isinstance(node, str):
        return None, node
    if isinstance(node, dict):
        rule = node.get("rule") or node.get("code")  # type: ignore[reportUnknownMemberType]
        reason = node.get("reason") or node.get("message")  # type: ignore[reportUnknownMemberType]
        return (
            rule if isinstance(rule, str) else None,
            reason if isinstance(reason, str) else None,
        )
    return None, None
