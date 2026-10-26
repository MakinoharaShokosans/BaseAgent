"""bash 工具（sidecar 薄客户端）。出处：03_shell_tool.md §1，02_shell_sandbox.md §2。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tools.builtin import BashTool
from tools.core import SidecarClient


def reply(**kw: Any) -> dict[str, Any]:
    return {
        "status": "SUCCESS",
        "exit_code": 0,
        "distilled_stdout": "",
        "distilled_stderr": "",
        "is_truncated": False,
        "artifact_path": None,
        "execution_time_ms": 12,
        "timed_out": False,
        **kw,
    }


class Rig:
    def __init__(
        self, status: int = 200, body: Any = None, *, boom: Exception | None = None
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self._status, self._body, self._boom = status, body if body is not None else reply(), boom
        self.client = SidecarClient(
            "http://127.0.0.1:8002", timeout_sec=5, transport=httpx.MockTransport(self._handle)
        )
        self.tool = BashTool(
            self.client,
            workspace_id="ws1",
            workspace_root="/work/proj",
            task_id="task1",
            default_timeout_sec=60,
            max_timeout_sec=600,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self._boom is not None:
            raise self._boom
        self.requests.append(json.loads(request.content))
        return httpx.Response(self._status, json=self._body)


@pytest.mark.unit
async def test_a_command_is_sent_with_the_task_context_and_stdout_becomes_the_content() -> None:
    rig = Rig(body=reply(distilled_stdout="hello\n"))
    assert rig.tool.idempotent is False and rig.tool.trust == "trusted"
    out = await rig.tool.invoke({"command": "echo hello"})
    assert out.ok and out.exit_code == 0 and out.content == "hello\n"
    assert rig.requests == [
        {
            "workspace_id": "ws1",
            "workspace_root": "/work/proj",
            "task_id": "task1",
            "step_id": 1,
            "command": "echo hello",
        }
    ]
    assert out.duration_ms == 12 and out.meta["execution_time_ms"] == 12


@pytest.mark.unit
async def test_step_ids_increase_per_tool_instance_and_are_independent_of_the_loop() -> None:
    rig = Rig()
    for _ in range(3):
        await rig.tool.invoke({"command": "true"})
    assert [r["step_id"] for r in rig.requests] == [1, 2, 3]


@pytest.mark.unit
@pytest.mark.parametrize("args", [{}, {"command": ""}, {"command": "   "}, {"command": 5}])
async def test_a_missing_or_empty_command_fails_without_calling_the_sidecar(
    args: dict[str, Any],
) -> None:
    rig = Rig()
    out = await rig.tool.invoke(args)
    assert not out.ok and "command" in (out.error or "")
    assert rig.requests == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("given", "sent"),
    [
        (30, 30),
        (30.5, 30.5),
        (9999, 600),
        ("abc", None),
        (-1, None),
        (0, None),
        (True, None),
        (None, None),
    ],
)
async def test_the_timeout_is_clamped_and_an_invalid_one_falls_back_to_the_default(
    given: Any, sent: float | None
) -> None:
    rig = Rig()
    await rig.tool.invoke({"command": "x", "timeout_sec": given})
    assert rig.requests[0].get("timeout_sec") == sent


@pytest.mark.unit
async def test_the_tool_deadline_outlasts_the_longest_command() -> None:
    assert Rig().tool.timeout_sec > 600


@pytest.mark.unit
async def test_stderr_follows_stdout_and_empty_output_is_said_so() -> None:
    both = await Rig(body=reply(distilled_stdout="out", distilled_stderr="warn")).tool.invoke(
        {"command": "x"}
    )
    assert both.content == "out\n[stderr]\nwarn"
    only_err = await Rig(body=reply(distilled_stderr="warn")).tool.invoke({"command": "x"})
    assert only_err.content == "[stderr]\nwarn"
    nothing = await Rig().tool.invoke({"command": "x"})
    assert nothing.ok and nothing.content == "（无输出）"


@pytest.mark.unit
async def test_a_nonzero_exit_is_not_ok_but_the_output_is_still_there() -> None:
    out = await Rig(
        body=reply(status="FAILED", exit_code=2, distilled_stderr="boom: no such file")
    ).tool.invoke({"command": "ls nope"})
    assert not out.ok and out.exit_code == 2 and out.failed
    assert "no such file" in out.content


@pytest.mark.unit
async def test_a_signal_death_is_a_failure_with_the_negative_code() -> None:
    out = await Rig(body=reply(status="KILLED", exit_code=-9)).tool.invoke({"command": "x"})
    assert not out.ok and out.exit_code == -9


@pytest.mark.unit
async def test_a_timeout_says_so_and_keeps_what_was_printed() -> None:
    out = await Rig(
        body=reply(status="TIMEOUT", exit_code=-15, timed_out=True, distilled_stdout="partial")
    ).tool.invoke({"command": "sleep 99", "timeout_sec": 5})
    assert not out.ok and out.failed
    assert "partial" in out.content and "超时" in out.content and "5" in out.content


@pytest.mark.unit
async def test_a_timeout_without_an_exit_code_is_still_a_failure() -> None:
    out = await Rig(body=reply(status="TIMEOUT", exit_code=None, timed_out=True)).tool.invoke(
        {"command": "x"}
    )
    assert not out.ok and out.failed


@pytest.mark.unit
async def test_queueing_is_ok_and_not_a_failure() -> None:
    out = await Rig(
        body=reply(status="QUEUED", exit_code=None, queue_wait_sec=0, estimated_wait_sec=12.5)
    ).tool.invoke({"command": "make"})
    assert out.ok and out.failed is False
    assert "等待队列" in out.content and "12" in out.content
    assert out.meta["queued"] is True


@pytest.mark.unit
async def test_the_artifact_handle_is_the_log_file_stem() -> None:
    out = await Rig(
        body=reply(
            distilled_stdout="head",
            is_truncated=True,
            artifact_path="/store/artifacts/task1/step_3_bash.log",
        )
    ).tool.invoke({"command": "cat big"})
    assert out.artifact_id == "step_3_bash"
    assert out.artifact_path == "/store/artifacts/task1/step_3_bash.log"
    assert out.is_truncated is True


@pytest.mark.unit
async def test_an_audit_rejection_carries_its_rule_and_reason_to_the_model() -> None:
    body = {
        "detail": {
            "code": "AUDIT_REJECTED",
            "rule": "RM_RF_ROOT",
            "reason": "禁止递归强制删除根目录",
        }
    }
    out = await Rig(status=403, body=body).tool.invoke({"command": "rm -rf /"})
    assert not out.ok and out.meta["policy_rejected"] is True
    assert out.meta["rule"] == "RM_RF_ROOT" and out.meta["status"] == 403
    assert "RM_RF_ROOT" in out.content and "禁止递归强制删除根目录" in out.content
    assert "不可达" not in out.content  # 与基础设施故障区分开


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        {"rule": "MKFS", "reason": "格式化"},
        {"detail": {"rule": "MKFS", "reason": "格式化"}},
        {"detail": [{"rule": "MKFS", "reason": "格式化"}]},
    ],
)
async def test_the_rejection_is_read_from_the_shapes_the_sidecar_may_use(body: Any) -> None:
    out = await Rig(status=403, body=body).tool.invoke({"command": "mkfs"})
    assert "MKFS" in out.content and "格式化" in out.content


@pytest.mark.unit
async def test_a_rejection_with_an_unreadable_body_still_says_it_was_rejected() -> None:
    out = await Rig(status=403, body={"detail": "nope"}).tool.invoke({"command": "x"})
    assert not out.ok and "安全策略拒绝" in out.content and "nope" in out.content


@pytest.mark.unit
async def test_a_path_escape_is_explained_as_such() -> None:
    body = {"detail": {"code": "PATH_ESCAPE_DETECTED", "reason": "写入目标 /etc/x 在工作区之外"}}
    out = await Rig(status=422, body=body).tool.invoke({"command": "echo > /etc/x"})
    assert not out.ok and out.meta["policy_rejected"] is True
    assert "/etc/x" in out.content and "工作区" in out.content


@pytest.mark.unit
@pytest.mark.parametrize("status", [404, 500, 502])
async def test_other_statuses_are_an_unreachable_sidecar(status: int) -> None:
    out = await Rig(status=status, body={"detail": "x"}).tool.invoke({"command": "x"})
    assert not out.ok and "sidecar" in out.content
    assert out.meta["dependency_unavailable"] is True


@pytest.mark.unit
async def test_a_connection_failure_is_an_observation_not_an_exception() -> None:
    out = await Rig(boom=httpx.ConnectError("refused")).tool.invoke({"command": "x"})
    assert not out.ok and "sidecar" in out.content and out.meta["dependency_unavailable"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "body", [{"status": "WHATEVER"}, {"status": "SUCCESS", "surprise": 1}, {"exit_code": 0}]
)
async def test_a_response_that_breaks_the_contract_is_rejected_not_trusted(body: Any) -> None:
    out = await Rig(body=body).tool.invoke({"command": "x"})
    assert not out.ok and "响应" in out.content
