"""评测运行器：通过 HTTP 把用例当作真实任务提交给一个运行中的服务，再按事件与工作区算结果。

它是黑盒客户端：只用公开的 API，不导入运行时，所以评测的就是用户真正会用到的那一套。
预算保护：单用例有 Token 与时间的硬上限（超过就取消任务），总 Token 超过上限后不再开始新的用例。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from evaluation.agent_bench.cases import Case
from evaluation.agent_bench.checks import CheckResult, run_checks
from evaluation.agent_bench.metrics import CaseMetrics, final_event, parse_sse, summarize_events

TERMINAL = frozenset({"succeeded", "terminated", "failed", "cancelled"})
_DELIVERY_CLIP = 2000


@dataclass(slots=True)
class CaseResult:
    case_id: str
    category: str
    repeat: int
    success: bool
    status: str
    termination_reason: str = ""
    steps: int = 0
    total_tokens: int = 0
    wall_sec: float = 0.0
    checks: list[CheckResult] = field(default_factory=list[CheckResult])
    metrics: CaseMetrics = field(default_factory=CaseMetrics)
    delivery: str = ""
    task_id: str = ""
    #: 评测端自己的问题（任务没跑起来、被预算取消……），区别于“任务失败”
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "repeat": self.repeat,
            "success": self.success,
            "status": self.status,
            "termination_reason": self.termination_reason,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "wall_sec": round(self.wall_sec, 2),
            "checks": [c.to_dict() for c in self.checks],
            "metrics": self.metrics.to_dict(),
            "delivery": self.delivery,
            "task_id": self.task_id,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class BenchOptions:
    poll_sec: float = 0.5
    default_timeout_sec: float = 600.0
    default_max_tokens: int = 50_000
    #: 所有用例累计 Token 的上限；到了就不再开始新的用例
    max_total_tokens: int = 1_000_000
    #: 跑完是否删除评测创建的工作区（会话与任务一起删）
    cleanup: bool = True


Progress = Callable[[CaseResult], Awaitable[None] | None]


class BenchRunner:
    def __init__(
        self,
        client: httpx.AsyncClient,
        workdir: Path,
        *,
        options: BenchOptions | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._workdir = workdir
        self._opts = options or BenchOptions()
        self._clock = clock
        self.spent_tokens = 0

    async def run(
        self, cases: Sequence[Case], *, repeat: int = 1, progress: Progress | None = None
    ) -> list[CaseResult]:
        results: list[CaseResult] = []
        for case in cases:
            for index in range(repeat):
                if self.spent_tokens >= self._opts.max_total_tokens:
                    result = CaseResult(
                        case.id,
                        case.category,
                        index,
                        False,
                        "skipped",
                        note="总 Token 预算已用完，未运行",
                    )
                else:
                    result = await self.run_case(case, index)
                results.append(result)
                if progress is not None:
                    maybe = progress(result)
                    if maybe is not None:
                        await maybe
        return results

    async def run_case(self, case: Case, repeat: int = 0) -> CaseResult:
        root = self._workdir / f"{case.id}-{repeat}"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        for rel, content in case.files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        result = CaseResult(case.id, case.category, repeat, False, "error")
        workspace_id = ""
        started = self._clock()
        try:
            workspace_id, task = await self._submit(case, root, repeat)
            result.task_id = str(task["task_id"])
            final = await self._wait(case, result.task_id, started)
            result.status = str(final["status"])
            result.termination_reason = str(final.get("termination_reason", ""))
            result.steps = int(final.get("step_count", 0))
            result.total_tokens = int(final.get("total_tokens", 0))
            events = parse_sse((await self._get(f"/tasks/{result.task_id}/stream?after=0")).text)
            result.metrics = summarize_events(events)
            result.delivery = str(final_event(events).get("delivery", ""))[:_DELIVERY_CLIP]
            result.checks = await run_checks(case, root, result.delivery)
            result.success = result.status in case.expect_status and all(
                c.passed for c in result.checks
            )
        except _BenchError as exc:
            result.note = str(exc)
        except httpx.HTTPError as exc:
            result.note = f"请求失败：{type(exc).__name__}: {exc}"
        finally:
            result.wall_sec = self._clock() - started
            self.spent_tokens += result.total_tokens
            if self._opts.cleanup and workspace_id:
                await self._cleanup(workspace_id)
        return result

    # ------------------------------------------------------------------ 与服务交互

    async def _submit(self, case: Case, root: Path, repeat: int) -> tuple[str, dict[str, Any]]:
        name = f"bench-{case.id}-{repeat}-{int(time.time())}"
        ws = await self._post("/workspaces", {"name": name, "root_path": str(root)})
        workspace_id = str(ws["workspace_id"])
        session = await self._post(f"/workspaces/{workspace_id}/sessions")
        task = await self._post(
            f"/sessions/{session['session_id']}/tasks",
            {"task_goal": case.goal, "permission_level": case.permission_level},
        )
        return workspace_id, task

    async def _wait(self, case: Case, task_id: str, started: float) -> dict[str, Any]:
        limit = case.timeout_sec or self._opts.default_timeout_sec
        max_tokens = case.max_tokens or self._opts.default_max_tokens
        decided: set[str] = set()
        cancelled = ""
        while True:
            task = (await self._get(f"/tasks/{task_id}")).json()
            if task["status"] in TERMINAL:
                if cancelled:
                    task["termination_reason"] = cancelled
                return dict(task)
            request = task.get("approval_request")
            if task["status"] == "waiting_for_approval" and request:
                approval_id = str(request["approval_id"])
                if approval_id not in decided:
                    decided.add(approval_id)
                    await self._decide(task_id, approval_id, case.approve)
            if not cancelled:
                if self._clock() - started > limit:
                    cancelled = f"评测超时（{limit:g} 秒），已取消"
                elif int(task.get("total_tokens", 0)) > max_tokens:
                    cancelled = f"评测 Token 上限（{max_tokens}）已超，已取消"
                if cancelled:
                    await self._post(f"/tasks/{task_id}/cancel")
            await asyncio.sleep(self._opts.poll_sec)

    async def _decide(self, task_id: str, approval_id: str, approve: bool) -> None:
        if approve:
            await self._post(
                f"/tasks/{task_id}/approve", {"approval_id": approval_id, "decision": "once"}
            )
        else:
            await self._post(
                f"/tasks/{task_id}/reject",
                {"approval_id": approval_id, "reason": "评测默认拒绝需要审批的操作"},
            )

    async def _cleanup(self, workspace_id: str) -> None:
        with contextlib.suppress(httpx.HTTPError):  # 清理失败不影响结果
            await self._client.delete(f"/workspaces/{workspace_id}")

    async def _get(self, path: str) -> httpx.Response:
        response = await self._client.get(path)
        if response.status_code >= 400:
            raise _BenchError(f"GET {path} → {response.status_code}：{response.text[:200]}")
        return response

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        response = (
            await self._client.post(path, json=body)
            if body is not None
            else await self._client.post(path)
        )
        if response.status_code >= 400:
            raise _BenchError(f"POST {path} → {response.status_code}：{response.text[:200]}")
        return dict(response.json()) if response.content else {}


class _BenchError(Exception):
    """评测端能解释的问题：服务拒绝了请求、任务没起来。"""
