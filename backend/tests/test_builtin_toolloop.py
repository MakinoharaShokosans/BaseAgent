"""真实内置工具接进工具包装层：产物回取闭环、路径与权限、不可信信封。"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_runtime.approval import check_binding, compute_approval_id
from agent_runtime.context import ObservationGovernor, ObservationLimits, ObservationStore
from agent_runtime.core import (
    AgentState,
    ApprovalRequired,
    Message,
    ToolCall,
    apply_update,
    unanswered_calls,
)
from agent_runtime.guardrails import LoopLimits
from agent_runtime.loop import ApprovalHooks, ToolRunnerDeps, make_tool_runner
from gateway_support import MakeState, with_state
from test_permissions import POLICY
from tools.builtin import FileLimits, ShellBinding, TaskArtifacts, build_builtin_tools
from tools.core import SidecarClient, ToolDispatcher, ToolRegistry

OBS = ObservationLimits(
    max_tokens=200,
    head_lines=3,
    tail_lines=4,
    keyword_lines_limit=5,
    json_outline_max_depth=3,
    json_outline_array_items=2,
    scalar_preview_chars=10,
    exempt_tools=("load_skill",),
    exempt_multiplier=4,
)
POLICY_WITH_ARTIFACTS = dataclasses.replace(
    POLICY, read_only_tools=POLICY.read_only_tools | {"read_artifact", "view_file"}
)
FILES = FileLimits(view_max_lines=50, view_max_file_bytes=10**6, write_max_bytes=10**6)


class World:
    def __init__(self, tmp_path: Path, sidecar: Mapping[str, Any] | None = None) -> None:
        self.workspace = (tmp_path / "ws").resolve()
        self.workspace.mkdir(exist_ok=True)
        self.store = (tmp_path / "artifacts").resolve()
        self.artifacts = TaskArtifacts()
        self.requests: list[httpx.Request] = []
        body = sidecar or {"status": "SUCCESS", "exit_code": 0, "distilled_stdout": "ok"}
        client = SidecarClient(
            "http://127.0.0.1:8002",
            timeout_sec=5,
            transport=httpx.MockTransport(lambda r: self._answer(r, body)),
        )
        tools = build_builtin_tools(
            workspace_root=self.workspace,
            files=FILES,
            artifacts=self.artifacts,
            artifacts_root=self.store,
            grep_max_results=50,
            shell=ShellBinding(client, "ws", str(self.workspace), "t1", 60, 600),
        )
        self.runner = make_tool_runner(
            ToolRunnerDeps(
                dispatcher=ToolDispatcher(ToolRegistry(tools)),
                policy=POLICY_WITH_ARTIFACTS,
                limits=LoopLimits(3, 3),
                governor=ObservationGovernor(OBS, len, ObservationStore(self.store)),
                approval=ApprovalHooks(compute_approval_id, check_binding),
                bind=lambda s: self.artifacts.bind(s.artifacts),
            )
        )

    def _answer(self, request: httpx.Request, body: Mapping[str, Any]) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=dict(body))

    async def run(self, state: AgentState) -> AgentState:
        return apply_update(state, dict(await self.runner(state, unanswered_calls(state.messages))))


def turn(
    make_state: MakeState, *calls: tuple[str, str, Mapping[str, Any]], **fields: Any
) -> AgentState:
    base: dict[str, Any] = {
        "messages": (
            Message(role="user", content="go"),
            Message(
                role="assistant",
                content="",
                tool_calls=tuple(ToolCall(id=i, name=n, args=a) for i, n, a in calls),
            ),
        ),
    }
    return with_state(make_state(permission_level="workspace_write"), **{**base, **fields})


def reply_to(state: AgentState, call_id: str) -> str:
    return next(m.content for m in state.messages if m.role == "tool" and m.tool_call_id == call_id)


@pytest.mark.unit
async def test_a_written_file_can_be_viewed_in_the_next_turn(
    make_state: MakeState, tmp_path: Path
) -> None:
    world = World(tmp_path)
    state = await world.run(
        turn(make_state, ("w1", "write_file", {"path": "src/a.py", "content": "x = 1\ny = 2\n"}))
    )
    assert (world.workspace / "src" / "a.py").read_text() == "x = 1\ny = 2\n"
    assert "已写入" in reply_to(state, "w1")

    state = await world.run(
        with_state(
            state,
            messages=(
                *state.messages,
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall(id="v1", name="view_file", args={"path": "src/a.py"}),),
                ),
            ),
        )
    )
    assert "     2\ty = 2" in reply_to(state, "v1")


@pytest.mark.unit
async def test_truncated_bash_output_can_be_fetched_back_with_read_artifact(
    make_state: MakeState, tmp_path: Path
) -> None:
    """缺口闭环：sidecar 的全量日志被登记，模型据摘要里的句柄取回。"""
    log = (tmp_path / "artifacts").resolve() / "t1" / "step_1_bash.log"
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"row {i}\n" for i in range(1, 121)), encoding="utf-8")
    world = World(
        tmp_path,
        {
            "status": "SUCCESS",
            "exit_code": 0,
            "distilled_stdout": "row 1\n...\nrow 120",
            "is_truncated": True,
            "artifact_path": str(log),
        },
    )
    after = await world.run(turn(make_state, ("b1", "bash", {"command": "seq 120"})))
    assert "artifact://step_1_bash" in reply_to(after, "b1")
    assert after.artifacts == {"step_1_bash": str(log)}

    again = with_state(
        after,
        messages=(
            *after.messages,
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(
                        id="r1",
                        name="read_artifact",
                        args={"artifact_id": "step_1_bash", "start_line": 60, "end_line": 61},
                    ),
                ),
            ),
        ),
    )
    final = await world.run(again)
    assert "row 60" in reply_to(final, "r1") and "row 61" in reply_to(final, "r1")


@pytest.mark.unit
async def test_a_big_view_is_governed_and_its_handle_leads_back_to_the_rest(
    make_state: MakeState, tmp_path: Path
) -> None:
    world = World(tmp_path)
    (world.workspace / "big.txt").write_text(
        "\n".join(f"line {i:04d} padding padding" for i in range(50))
    )
    after = await world.run(turn(make_state, ("v1", "view_file", {"path": "big.txt"})))
    text = reply_to(after, "v1")
    assert "artifact://obs_step_1_view_file" in text
    follow = with_state(
        after,
        messages=(
            *after.messages,
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(
                        id="g1",
                        name="read_artifact",
                        args={"artifact_id": "obs_step_1_view_file", "grep": "line 0042"},
                    ),
                ),
            ),
        ),
    )
    assert "line 0042" in reply_to(await world.run(follow), "g1")


@pytest.mark.unit
async def test_a_path_escape_is_a_failure_observation_and_touches_nothing(
    make_state: MakeState, tmp_path: Path
) -> None:
    world = World(tmp_path)
    after = await world.run(
        turn(make_state, ("w1", "write_file", {"path": "../evil.txt", "content": "x"}))
    )
    assert "工作区" in reply_to(after, "w1")
    assert not (tmp_path / "evil.txt").exists()
    assert after.consecutive_errors == 1


@pytest.mark.unit
async def test_a_policy_rejected_command_reaches_the_model_with_its_reason(
    make_state: MakeState, tmp_path: Path
) -> None:
    world = World(tmp_path)
    world._answer = lambda request, body: httpx.Response(  # type: ignore[method-assign]
        403, json={"detail": {"rule": "RM_RF_ROOT", "reason": "禁止递归强制删除根目录"}}
    )
    after = await world.run(turn(make_state, ("b1", "bash", {"command": "ls"})))
    assert "RM_RF_ROOT" in reply_to(after, "b1")


@pytest.mark.unit
async def test_bash_that_needs_more_than_the_baseline_still_stops_for_approval(
    make_state: MakeState, tmp_path: Path
) -> None:
    world = World(tmp_path)
    with pytest.raises(ApprovalRequired):
        await world.run(turn(make_state, ("b1", "bash", {"command": "git push origin main"})))
    assert world.requests == []
