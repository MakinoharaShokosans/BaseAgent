"""全局事件流（GET /events）与会话搜索测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from api_support import HEADERS, ApiRig, build_rig

V1 = "/api/v1"


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    async for r in build_rig(tmp_path):
        yield r


@pytest.mark.unit
async def test_global_stream_receives_task_events(rig: ApiRig) -> None:
    sid = await rig.create_session()
    hub = rig.services.hub

    received: list[str] = []

    async def reader() -> None:
        async for event in hub.global_stream(heartbeat_sec=1.0):
            if event.name != "heartbeat":
                received.append(event.name)
                if event.name == "task.finished":
                    break

    task = asyncio.create_task(reader())
    # 稍作等待确保订阅者已挂载
    await asyncio.sleep(0.05)

    # 模拟任务生命周期发出事件
    await hub.emit(
        "t_test1",
        "task.started",
        {"session_id": sid, "workspace_id": "ws_1", "task_goal": "测试目标"},
    )
    await hub.emit(
        "t_test1",
        "task.finished",
        {"session_id": sid, "workspace_id": "ws_1", "status": "completed"},
    )

    await asyncio.wait_for(task, timeout=3.0)
    assert received == ["task.started", "task.finished"]


@pytest.mark.unit
async def test_session_search_endpoint(rig: ApiRig) -> None:
    sid1 = await rig.create_session()
    sid2 = await rig.create_session()

    await rig.client.patch(
        f"{V1}/sessions/{sid1}",
        json={"title": "重构前端路由体系", "pinned": True},
        headers=HEADERS,
    )
    await rig.client.patch(
        f"{V1}/sessions/{sid2}",
        json={"title": "修复数据库连接泄漏"},
        headers=HEADERS,
    )

    # 搜索 "路由"
    resp = await rig.client.get(f"{V1}/sessions/search?q=路由", headers=HEADERS)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["session_id"] == sid1
    assert items[0]["title"] == "重构前端路由体系"

    # 搜索不存在的内容
    resp_empty = await rig.client.get(f"{V1}/sessions/search?q=不存在的关键字", headers=HEADERS)
    assert resp_empty.status_code == 200
    assert resp_empty.json() == []


@pytest.mark.unit
async def test_tool_output_transient_event(rig: ApiRig) -> None:
    from agent_runtime.runtime.events import TaskEvents

    events = TaskEvents(
        rig.services.hub,
        "task_tool_test",
        rig.services.redactor,
        turn_offset=0,
    )

    received: list[dict] = []  # type: ignore[type-arg]

    async def listener() -> None:
        async for ev in rig.services.hub.stream("task_tool_test", 0, heartbeat_sec=1.0):
            if ev.name == "tool.output":
                received.append(ev.data)
                break

    task = asyncio.create_task(listener())
    await asyncio.sleep(0.05)

    await events.tool_output("call_123", "stdout", "npm install finished in 1.2s")
    await asyncio.wait_for(task, timeout=3.0)

    assert len(received) == 1
    assert received[0]["call_id"] == "call_123"
    assert received[0]["stream"] == "stdout"
    assert "npm install" in str(received[0]["text"])
