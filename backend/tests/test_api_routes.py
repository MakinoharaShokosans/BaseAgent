"""端点行为：工作区、会话、任务、审批、事件流、产物。出处：docs/09_api/01_endpoints.md §11。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.approval import compute_approval_id
from agent_runtime.memory import ExtractionRequest, SessionCompactor
from agent_runtime.memory.merge import Extraction
from agent_runtime.runtime import compaction_config_from_settings
from api_support import HEADERS, ApiRig, build_rig

V1 = "/api/v1"


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    async for r in build_rig(tmp_path):
        yield r


async def settle(rig: ApiRig, task_id: str) -> None:
    await rig.services.tasks.runner.wait(task_id)


def err(response: Any) -> dict[str, Any]:
    return dict(response.json()["error"])


# ==============================================================================
# 工作区
# ==============================================================================


@pytest.mark.unit
async def test_workspace_lifecycle(rig: ApiRig) -> None:
    created = await rig.client.post(
        f"{V1}/workspaces",
        json={"name": "Aegis", "root_path": str(rig.root), "description": "d"},
        headers=HEADERS,
    )
    assert created.status_code == 201
    ws = created.json()
    assert ws["root_path"] == str(rig.root.resolve()) and ws["created_at"] > 0
    wid = ws["workspace_id"]

    assert [
        w["workspace_id"]
        for w in (await rig.client.get(f"{V1}/workspaces", headers=HEADERS)).json()
    ] == [wid]
    patched = await rig.client.patch(
        f"{V1}/workspaces/{wid}", json={"name": "新名"}, headers=HEADERS
    )
    assert patched.json()["name"] == "新名" and patched.json()["description"] == "d"
    assert (await rig.client.delete(f"{V1}/workspaces/{wid}", headers=HEADERS)).status_code == 204
    assert (
        err(await rig.client.get(f"{V1}/workspaces/{wid}", headers=HEADERS))["code"]
        == "WORKSPACE_NOT_FOUND"
    )


@pytest.mark.unit
async def test_creating_the_same_path_again_is_idempotent_unless_another_id_is_demanded(
    rig: ApiRig,
) -> None:
    body = {"name": "a", "root_path": str(rig.root)}
    first = (await rig.client.post(f"{V1}/workspaces", json=body, headers=HEADERS)).json()
    again = await rig.client.post(f"{V1}/workspaces", json=body, headers=HEADERS)
    assert again.status_code == 200 and again.json()["workspace_id"] == first["workspace_id"]
    clash = await rig.client.post(
        f"{V1}/workspaces", json={**body, "workspace_id": "other_id"}, headers=HEADERS
    )
    assert clash.status_code == 409 and err(clash)["code"] == "WORKSPACE_PATH_CONFLICT"


@pytest.mark.security
@pytest.mark.parametrize("raw", ["/", "/etc", "/usr/lib", "relative/dir", "/definitely/not/here"])
async def test_dangerous_or_invalid_workspace_roots_are_refused(rig: ApiRig, raw: str) -> None:
    response = await rig.client.post(
        f"{V1}/workspaces", json={"name": "x", "root_path": raw}, headers=HEADERS
    )
    assert response.status_code == 422 and err(response)["code"] == "WORKSPACE_PATH_INVALID"


@pytest.mark.security
async def test_the_home_directory_itself_and_files_are_refused(rig: ApiRig, tmp_path: Path) -> None:
    home = rig.services.home
    home.mkdir()
    for raw in (str(home), str(rig.root / "nofile")):
        r = await rig.client.post(
            f"{V1}/workspaces", json={"name": "x", "root_path": raw}, headers=HEADERS
        )
        assert r.status_code == 422
    (rig.root / "f.txt").write_text("x")
    r = await rig.client.post(
        f"{V1}/workspaces",
        json={"name": "x", "root_path": str(rig.root / "f.txt")},
        headers=HEADERS,
    )
    assert r.status_code == 422
    sub = home / "proj"
    sub.mkdir()
    ok = await rig.client.post(
        f"{V1}/workspaces", json={"name": "x", "root_path": str(sub)}, headers=HEADERS
    )
    assert ok.status_code == 201


@pytest.mark.security
async def test_a_symlink_to_a_system_directory_is_refused(rig: ApiRig) -> None:
    link = rig.root / "etc_link"
    link.symlink_to("/etc")
    r = await rig.client.post(
        f"{V1}/workspaces", json={"name": "x", "root_path": str(link)}, headers=HEADERS
    )
    assert r.status_code == 422


@pytest.mark.unit
async def test_workspace_memory_add_dedupe_and_delete(rig: ApiRig) -> None:
    wid = await rig.create_workspace()
    base = f"{V1}/workspaces/{wid}/memory"
    first = await rig.client.post(
        f"{base}/facts", json={"text": "用 pytest", "layer": "guidelines"}, headers=HEADERS
    )
    assert first.json()["guidelines"] == ["用 pytest"]
    again = await rig.client.post(
        f"{base}/facts", json={"text": "用 pytest", "layer": "guidelines"}, headers=HEADERS
    )
    assert again.json()["guidelines"] == ["用 pytest"]
    pit = await rig.client.post(
        f"{base}/failures", json={"text": "别直接改 lock 文件"}, headers=HEADERS
    )
    assert pit.json()["pitfalls"] == ["别直接改 lock 文件"]
    removed = await rig.client.delete(f"{base}/guidelines/0", headers=HEADERS)
    assert removed.json()["guidelines"] == []
    missing = await rig.client.delete(f"{base}/guidelines/0", headers=HEADERS)
    assert missing.status_code == 404 and err(missing)["code"] == "MEMORY_ITEM_NOT_FOUND"
    bad = await rig.client.delete(f"{base}/nonsense/0", headers=HEADERS)
    assert bad.status_code == 400


# ==============================================================================
# 会话
# ==============================================================================


@pytest.mark.unit
async def test_session_lifecycle_and_turn_paging(rig: ApiRig) -> None:
    sid = await rig.create_session()
    assert (await rig.client.get(f"{V1}/sessions/{sid}", headers=HEADERS)).json()[
        "session_id"
    ] == sid
    wid = (await rig.client.get(f"{V1}/sessions/{sid}", headers=HEADERS)).json()["workspace_id"]
    listed = await rig.client.get(f"{V1}/workspaces/{wid}/sessions", headers=HEADERS)
    assert [s["session_id"] for s in listed.json()] == [sid]
    for i in range(5):
        await rig.services.memory.append_turn(
            sid, "t", "user" if i % 2 == 0 else "assistant", f"m{i}", 1
        )
    page = (await rig.client.get(f"{V1}/sessions/{sid}/turns?limit=2", headers=HEADERS)).json()
    assert [t["content"] for t in page] == ["m3", "m4"]
    older = (
        await rig.client.get(
            f"{V1}/sessions/{sid}/turns?limit=2&before_id={page[0]['id']}", headers=HEADERS
        )
    ).json()
    assert [t["content"] for t in older] == ["m1", "m2"]
    memory = (await rig.client.get(f"{V1}/sessions/{sid}/memory", headers=HEADERS)).json()
    assert memory["summary"] == "" and memory["facts"] == []
    assert (await rig.client.delete(f"{V1}/sessions/{sid}", headers=HEADERS)).status_code == 204
    assert (
        err(await rig.client.get(f"{V1}/sessions/{sid}", headers=HEADERS))["code"]
        == "SESSION_NOT_FOUND"
    )


async def _wire_compactor(rig: ApiRig, result: Extraction | None) -> list[ExtractionRequest]:
    seen: list[ExtractionRequest] = []

    async def extract(request: ExtractionRequest) -> Extraction | None:
        seen.append(request)
        return result

    config = compaction_config_from_settings(rig.services.settings)
    rig.services.compact_session = SessionCompactor(
        rig.services.memory, extract, config
    ).compact_now
    return seen


@pytest.mark.unit
async def test_manual_memory_compaction(rig: ApiRig) -> None:
    sid = await rig.create_session()
    url = f"{V1}/sessions/{sid}/memory/compact"
    unwired = await rig.client.post(url, headers=HEADERS)
    assert unwired.status_code == 503 and err(unwired)["code"] == "DEPENDENCY_UNAVAILABLE"

    seen = await _wire_compactor(rig, Extraction("手动摘要", (), (), ()))
    for i in range(3):
        await rig.services.memory.append_turn(sid, f"t{i}", "user", f"q{i}", 5)
        await rig.services.memory.append_turn(sid, f"t{i}", "assistant", f"a{i}", 5)
    keep = rig.services.settings.context.session.active_window_turns
    done = await rig.client.post(url, headers=HEADERS)
    body = done.json()
    assert done.status_code == 200 and body["status"] == "compacted"
    assert body["turns"] == len(seen[0].turns) and body["memory"]["summary"] == "手动摘要"
    assert body["memory"]["watermark"] > 0 and keep >= 1
    again = (await rig.client.post(url, headers=HEADERS)).json()
    assert again["status"] == "nothing_to_compact" and again["turns"] == 0 and len(seen) == 1


@pytest.mark.unit
async def test_manual_compaction_failure_is_an_error_and_changes_nothing(rig: ApiRig) -> None:
    sid = await rig.create_session()
    await _wire_compactor(rig, None)
    for i in range(6):
        await rig.services.memory.append_turn(
            sid, f"t{i}", "user" if i % 2 == 0 else "assistant", f"m{i}", 5
        )
    failed = await rig.client.post(f"{V1}/sessions/{sid}/memory/compact", headers=HEADERS)
    assert failed.status_code == 502 and err(failed)["code"] == "COMPACTION_FAILED"
    memory = (await rig.client.get(f"{V1}/sessions/{sid}/memory", headers=HEADERS)).json()
    assert memory["summary"] == "" and memory["watermark"] == 0


@pytest.mark.unit
async def test_manual_compaction_is_refused_while_a_task_runs(rig: ApiRig) -> None:
    rig.world.gate = asyncio.Event()
    task = await rig.submit()
    await asyncio.wait_for(rig.world.entered.wait(), 5)
    await _wire_compactor(rig, Extraction("x", (), (), ()))
    response = await rig.client.post(
        f"{V1}/sessions/{task['session_id']}/memory/compact", headers=HEADERS
    )
    assert response.status_code == 409
    rig.world.gate.set()
    await settle(rig, task["task_id"])


@pytest.mark.unit
async def test_context_inspection_is_unavailable_until_wired(rig: ApiRig) -> None:
    sid = await rig.create_session()
    response = await rig.client.get(f"{V1}/sessions/{sid}/context", headers=HEADERS)
    assert response.status_code == 503 and err(response)["code"] == "DEPENDENCY_UNAVAILABLE"

    async def inspect(session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "layers": []}

    rig.services.context_inspector = inspect
    ok = await rig.client.get(f"{V1}/sessions/{sid}/context", headers=HEADERS)
    assert ok.json() == {"session_id": sid, "layers": []}


@pytest.mark.unit
async def test_deleting_a_session_cascades_to_its_tasks_and_events(rig: ApiRig) -> None:
    task = await rig.submit()
    await settle(rig, task["task_id"])
    assert await rig.services.hub.history(task["task_id"])
    gone = await rig.client.delete(f"{V1}/sessions/{task['session_id']}", headers=HEADERS)
    assert gone.status_code == 204
    assert await rig.services.hub.history(task["task_id"]) == []
    assert await rig.services.memory.get_task(task["task_id"]) is None
    assert (
        err(await rig.client.get(f"{V1}/tasks/{task['task_id']}", headers=HEADERS))["code"]
        == "TASK_NOT_FOUND"
    )


@pytest.mark.unit
async def test_deleting_a_workspace_with_a_running_task_is_a_conflict(rig: ApiRig) -> None:
    rig.world.gate = asyncio.Event()
    task = await rig.submit()
    await rig.world.entered.wait()
    r = await rig.client.delete(f"{V1}/workspaces/{task['workspace_id']}", headers=HEADERS)
    assert r.status_code == 409 and err(r)["code"] == "TASK_STATE_CONFLICT"
    rig.world.gate.set()
    await settle(rig, task["task_id"])
    assert (
        await rig.client.delete(f"{V1}/workspaces/{task['workspace_id']}", headers=HEADERS)
    ).status_code == 204
    assert await rig.services.hub.history(task["task_id"]) == []


# ==============================================================================
# 任务
# ==============================================================================


@pytest.mark.unit
async def test_submit_returns_202_and_a_full_task_snapshot(rig: ApiRig) -> None:
    rig.world.gate = asyncio.Event()
    task = await rig.submit("修复 bug")
    assert task["status"] == "running" and task["task_goal"] == "修复 bug"
    assert task["approval_request"] is None and task["milestones"] == []
    for key in (
        "task_id",
        "session_id",
        "workspace_id",
        "permission_level",
        "step_count",
        "total_tokens",
        "consecutive_errors",
        "should_terminate",
        "termination_reason",
        "created_at",
        "started_at",
        "finished_at",
        "artifact_count",
        "current_milestone_idx",
    ):
        assert key in task
    rig.world.gate.set()
    await settle(rig, task["task_id"])
    done = (await rig.client.get(f"{V1}/tasks/{task['task_id']}", headers=HEADERS)).json()
    assert done["status"] == "succeeded" and done["step_count"] == 1
    assert done["milestones"][0]["status"] == "completed"


@pytest.mark.unit
async def test_second_concurrent_task_is_429_and_recovers_after_the_first_finishes(
    rig: ApiRig,
) -> None:
    rig.world.gate = asyncio.Event()
    first = await rig.submit()
    sid = first["session_id"]
    blocked = await rig.client.post(
        f"{V1}/sessions/{sid}/tasks", json={"task_goal": "b"}, headers=HEADERS
    )
    assert blocked.status_code == 429 and err(blocked)["code"] == "TASK_QUEUE_FULL"
    rig.world.gate.set()
    await settle(rig, first["task_id"])
    ok = await rig.client.post(
        f"{V1}/sessions/{sid}/tasks", json={"task_goal": "c"}, headers=HEADERS
    )
    assert ok.status_code == 202
    await settle(rig, ok.json()["task_id"])


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"task_goal": ""},
        {"task_goal": "g", "permission_level": "root"},
        {"task_goal": "g", "x": 1},
    ],
)
async def test_bad_submit_bodies_are_400(rig: ApiRig, body: dict[str, Any]) -> None:
    sid = await rig.create_session()
    r = await rig.client.post(f"{V1}/sessions/{sid}/tasks", json=body, headers=HEADERS)
    assert r.status_code == 400 and err(r)["code"] == "VALIDATION_ERROR"


@pytest.mark.unit
async def test_goal_length_cap_and_whitespace_goal(rig: ApiRig) -> None:
    sid = await rig.create_session()
    limit = rig.services.settings.server.task_goal_max_chars
    long = await rig.client.post(
        f"{V1}/sessions/{sid}/tasks", json={"task_goal": "x" * (limit + 1)}, headers=HEADERS
    )
    assert long.status_code == 400
    blank = await rig.client.post(
        f"{V1}/sessions/{sid}/tasks", json={"task_goal": "   "}, headers=HEADERS
    )
    assert blank.status_code == 400 and err(blank)["code"] == "VALIDATION_ERROR"


@pytest.mark.security
async def test_full_permissions_needs_explicit_confirmation(rig: ApiRig) -> None:
    sid = await rig.create_session()
    body = {"task_goal": "g", "permission_level": "full_permissions"}
    refused = await rig.client.post(f"{V1}/sessions/{sid}/tasks", json=body, headers=HEADERS)
    assert refused.status_code == 422 and err(refused)["code"] == "FULL_PERMISSIONS_NOT_CONFIRMED"
    ok = await rig.client.post(
        f"{V1}/sessions/{sid}/tasks",
        json={**body, "confirm_full_permissions": True},
        headers=HEADERS,
    )
    assert ok.status_code == 202 and ok.json()["permission_level"] == "full_permissions"
    await settle(rig, ok.json()["task_id"])


@pytest.mark.unit
async def test_unknown_session_and_task_are_404(rig: ApiRig) -> None:
    r = await rig.client.post(f"{V1}/sessions/nope/tasks", json={"task_goal": "g"}, headers=HEADERS)
    assert r.status_code == 404 and err(r)["code"] == "SESSION_NOT_FOUND"
    for method, path in (
        ("GET", "tasks/x"),
        ("POST", "tasks/x/cancel"),
        ("POST", "tasks/x/resume"),
        ("GET", "tasks/x/stream"),
        ("GET", "tasks/x/timeline"),
        ("GET", "tasks/x/artifacts"),
        ("DELETE", "tasks/x"),
    ):
        response = await rig.client.request(method, f"{V1}/{path}", headers=HEADERS)
        assert response.status_code == 404 and err(response)["code"] == "TASK_NOT_FOUND", path


@pytest.mark.unit
async def test_list_tasks_of_a_session_newest_first(rig: ApiRig) -> None:
    first = await rig.submit("one")
    await settle(rig, first["task_id"])
    second = await rig.client.post(
        f"{V1}/sessions/{first['session_id']}/tasks", json={"task_goal": "two"}, headers=HEADERS
    )
    await settle(rig, second.json()["task_id"])
    listed = (
        await rig.client.get(f"{V1}/sessions/{first['session_id']}/tasks", headers=HEADERS)
    ).json()
    assert [t["task_goal"] for t in listed] == ["two", "one"]
    paged = await rig.client.get(
        f"{V1}/sessions/{first['session_id']}/tasks?limit=1&before={listed[0]['created_at']}",
        headers=HEADERS,
    )
    assert [t["task_goal"] for t in paged.json()] == ["one"]


@pytest.mark.unit
async def test_resume_cancel_delete_flow(rig: ApiRig) -> None:
    rig.world.terminate = True
    task = await rig.submit()
    tid = task["task_id"]
    await settle(rig, tid)
    assert (await rig.client.get(f"{V1}/tasks/{tid}", headers=HEADERS)).json()[
        "status"
    ] == "terminated"
    rig.world.terminate = False
    resumed = await rig.client.post(f"{V1}/tasks/{tid}/resume", headers=HEADERS)
    assert resumed.status_code == 202
    await settle(rig, tid)
    again = await rig.client.post(f"{V1}/tasks/{tid}/resume", headers=HEADERS)
    assert again.status_code == 409 and err(again)["code"] == "TASK_STATE_CONFLICT"
    cancelled = await rig.client.post(f"{V1}/tasks/{tid}/cancel", headers=HEADERS)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "succeeded"
    assert (await rig.client.delete(f"{V1}/tasks/{tid}", headers=HEADERS)).status_code == 204
    assert (await rig.client.get(f"{V1}/tasks/{tid}", headers=HEADERS)).status_code == 404


# ==============================================================================
# 审批
# ==============================================================================


async def waiting(rig: ApiRig) -> dict[str, Any]:
    rig.world.approval = True
    task = await rig.submit()
    await settle(rig, task["task_id"])
    return dict((await rig.client.get(f"{V1}/tasks/{task['task_id']}", headers=HEADERS)).json())


@pytest.mark.unit
async def test_waiting_task_snapshot_carries_the_request_and_approval_completes_it(
    rig: ApiRig,
) -> None:
    task = await waiting(rig)
    assert task["status"] == "waiting_for_approval"
    request = task["approval_request"]
    assert request["approval_id"] == compute_approval_id(task["task_id"], 1, ("c1",))
    assert request["action_type"] == "network_egress" and request["command"] == "curl x"
    r = await rig.client.post(
        f"{V1}/tasks/{task['task_id']}/approve",
        json={"approval_id": request["approval_id"], "decision": "once"},
        headers=HEADERS,
    )
    assert r.status_code == 202 and r.json()["task_id"] == task["task_id"]
    await settle(rig, task["task_id"])
    assert (await rig.client.get(f"{V1}/tasks/{task['task_id']}", headers=HEADERS)).json()[
        "status"
    ] == "succeeded"


@pytest.mark.unit
async def test_approval_id_is_required_and_a_wrong_one_is_409(rig: ApiRig) -> None:
    task = await waiting(rig)
    url = f"{V1}/tasks/{task['task_id']}/approve"
    missing = await rig.client.post(url, json={"decision": "once"}, headers=HEADERS)
    assert missing.status_code == 400
    wrong = await rig.client.post(url, json={"approval_id": "appr_wrong"}, headers=HEADERS)
    assert wrong.status_code == 409 and err(wrong)["code"] == "APPROVAL_MISMATCH"
    assert (await rig.client.get(f"{V1}/tasks/{task['task_id']}", headers=HEADERS)).json()[
        "status"
    ] == "waiting_for_approval"


@pytest.mark.unit
async def test_reject_and_idempotent_repeat(rig: ApiRig) -> None:
    task = await waiting(rig)
    aid = task["approval_request"]["approval_id"]
    url = f"{V1}/tasks/{task['task_id']}/reject"
    first = await rig.client.post(url, json={"approval_id": aid, "reason": "不行"}, headers=HEADERS)
    assert first.status_code == 202
    await settle(rig, task["task_id"])
    repeat = await rig.client.post(
        url, json={"approval_id": aid, "reason": "不行"}, headers=HEADERS
    )
    assert repeat.status_code == 202  # 重复提交返回当前状态而非错误
    flip = await rig.client.post(
        f"{V1}/tasks/{task['task_id']}/approve", json={"approval_id": aid}, headers=HEADERS
    )
    assert flip.status_code == 409


@pytest.mark.unit
async def test_approving_a_task_that_is_not_waiting_is_409(rig: ApiRig) -> None:
    task = await rig.submit()
    await settle(rig, task["task_id"])
    r = await rig.client.post(
        f"{V1}/tasks/{task['task_id']}/approve", json={"approval_id": "x"}, headers=HEADERS
    )
    assert r.status_code == 409 and err(r)["code"] == "TASK_STATE_CONFLICT"


# ==============================================================================
# 事件流
# ==============================================================================


def parse_sse(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in text.strip().split("\n\n"):
        item: dict[str, Any] = {}
        for line in block.splitlines():
            key, _, value = line.partition(": ")
            item[key] = value
        if item:
            frames.append(item)
    return frames


@pytest.mark.unit
async def test_stream_replays_a_finished_task_in_order_and_closes(rig: ApiRig) -> None:
    task = await rig.submit()
    await settle(rig, task["task_id"])
    response = await rig.client.get(f"{V1}/tasks/{task['task_id']}/stream", headers=HEADERS)
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(response.text)
    ids = [int(f["id"]) for f in frames]
    assert ids == list(range(1, len(ids) + 1))
    assert frames[0]["event"] == "task.started" and frames[-1]["event"] == "task.finished"
    last = json.loads(frames[-1]["data"])
    assert last["status"] == "succeeded" and last["seq"] == ids[-1] and last["v"] == 1
    assert all(json.loads(f["data"])["event"] == f["event"] for f in frames)


@pytest.mark.unit
async def test_node_finished_carries_a_stage_ordinal_that_is_not_the_envelope_seq(
    rig: ApiRig,
) -> None:
    """阶段序号和事件序号是两回事；同名会被信封覆盖，客户端就拿不到阶段序号。"""
    task = await rig.submit()
    await settle(rig, task["task_id"])
    text = (await rig.client.get(f"{V1}/tasks/{task['task_id']}/stream", headers=HEADERS)).text
    nodes = [json.loads(f["data"]) for f in parse_sse(text) if f["event"] == "node.finished"]
    assert [n["stage"] for n in nodes] == list(range(1, len(nodes) + 1))
    assert all(n["seq"] > n["stage"] for n in nodes)


@pytest.mark.unit
async def test_stream_resumes_from_last_event_id_without_duplicates(rig: ApiRig) -> None:
    task = await rig.submit()
    await settle(rig, task["task_id"])
    url = f"{V1}/tasks/{task['task_id']}/stream"
    everything = parse_sse((await rig.client.get(url, headers=HEADERS)).text)
    cut = int(everything[2]["id"])
    rest = parse_sse(
        (await rig.client.get(url, headers={**HEADERS, "Last-Event-ID": str(cut)})).text
    )
    assert [int(f["id"]) for f in rest] == [int(f["id"]) for f in everything if int(f["id"]) > cut]
    by_query = parse_sse((await rig.client.get(f"{url}?after={cut}", headers=HEADERS)).text)
    assert by_query == rest


@pytest.mark.unit
async def test_stream_of_a_future_cursor_reports_a_gap(rig: ApiRig) -> None:
    task = await rig.submit()
    await settle(rig, task["task_id"])
    r = await rig.client.get(
        f"{V1}/tasks/{task['task_id']}/stream", headers={**HEADERS, "Last-Event-ID": "9999"}
    )
    frames = parse_sse(r.text)
    assert (
        frames[0]["event"] == "task.error" and json.loads(frames[0]["data"])["code"] == "STREAM_GAP"
    )


@pytest.mark.security
async def test_stream_requires_the_token_and_events_carry_no_secrets(rig: ApiRig) -> None:
    task = await rig.submit()
    await settle(rig, task["task_id"])
    url = f"{V1}/tasks/{task['task_id']}/stream"
    assert (await rig.client.get(url)).status_code == 401
    text = (await rig.client.get(url, headers=HEADERS)).text
    assert "hunter2" not in text and "canary-" not in text  # 工具入参与观察值已脱敏


@pytest.mark.unit
async def test_stream_of_a_waiting_task_does_not_close_until_it_finishes(rig: ApiRig) -> None:
    task = await waiting(rig)
    url = f"{V1}/tasks/{task['task_id']}/stream"
    aid = task["approval_request"]["approval_id"]

    async def read() -> str:
        return (await rig.client.get(url, headers=HEADERS)).text

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.05)
    assert not reader.done()  # 挂起不是终态
    await rig.client.post(
        f"{V1}/tasks/{task['task_id']}/approve", json={"approval_id": aid}, headers=HEADERS
    )
    frames = parse_sse(await asyncio.wait_for(reader, 5))
    names = [f["event"] for f in frames]
    assert (
        names.index("task.waiting_for_approval")
        < names.index("task.approved")
        < names.index("task.finished")
    )


# ==============================================================================
# 时间线、轨迹、产物
# ==============================================================================


@pytest.mark.unit
async def test_timeline_trace_spans_and_llm_calls(rig: ApiRig) -> None:
    task = await rig.submit()
    await settle(rig, task["task_id"])
    tid = task["task_id"]
    timeline = (await rig.client.get(f"{V1}/tasks/{tid}/timeline?limit=50", headers=HEADERS)).json()
    assert timeline[0]["role"] == "user" and any(i["role"] == "tool" for i in timeline)
    assert "canary-" not in json.dumps(timeline)
    trace = await rig.client.get(f"{V1}/tasks/{tid}/trace", headers=HEADERS)
    assert trace.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in trace.text.splitlines()]
    assert lines[0]["event"] == "task.started" and lines[-1]["event"] == "task.finished"
    spans = (await rig.client.get(f"{V1}/tasks/{tid}/spans", headers=HEADERS)).json()
    tool = next(s for s in spans if s["kind"] == "tool")
    assert tool["name"] == "bash" and tool["ok"] is True and tool["ended_ts"] >= tool["ts"]
    assert (await rig.client.get(f"{V1}/tasks/{tid}/llm_calls", headers=HEADERS)).json() == []


@pytest.mark.unit
async def test_artifacts_list_and_download_and_traversal_is_refused(
    rig: ApiRig, tmp_path: Path
) -> None:
    folder = tmp_path / "artifacts" / "task-1"
    folder.mkdir(parents=True)
    (folder / "obs_1.log").write_text("完整输出")
    rig.world.artifact = folder / "obs_1.log"
    task = await rig.submit()
    await settle(rig, task["task_id"])
    tid = task["task_id"]
    listed = (await rig.client.get(f"{V1}/tasks/{tid}/artifacts", headers=HEADERS)).json()
    assert listed == [{"artifact_id": "obs_1", "size": len("完整输出".encode())}]
    got = await rig.client.get(f"{V1}/tasks/{tid}/artifacts/obs_1", headers=HEADERS)
    assert got.status_code == 200 and got.text == "完整输出"
    for bad in ("..%2F..%2Fetc%2Fpasswd", "nope", "obs_1/../x"):
        r = await rig.client.get(f"{V1}/tasks/{tid}/artifacts/{bad}", headers=HEADERS)
        assert r.status_code in (400, 404)
