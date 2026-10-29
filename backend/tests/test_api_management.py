"""界面设置覆盖层：会话元数据、提示词、工作区设置、技能与 MCP 开关。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_runtime.runtime.overrides import (
    load_mcp_policy,
    load_skill_policy,
    load_workspace_settings,
)
from api_support import HEADERS, ApiRig, build_rig

V1 = "/api/v1"


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    async for r in build_rig(tmp_path):
        yield r


@pytest.mark.unit
async def test_session_metadata_and_title(rig: ApiRig) -> None:
    sid = await rig.create_session()
    info = await rig.services.memory.get_session(sid)
    assert info.title is None and info.pinned is False and info.archived is False
    await rig.services.memory.append_turn(sid, "t1", "user", "把配置加载器里的旧兼容层删掉", 10)

    async def title(_: str) -> str:
        return "删除旧兼容层"

    rig.services.title_generator = title
    response = await rig.client.post(f"{V1}/sessions/{sid}/title", headers=HEADERS)
    assert response.status_code == 200 and response.json()["title"] == "删除旧兼容层"

    patched = await rig.client.patch(
        f"{V1}/sessions/{sid}", json={"pinned": True, "title": "新标题"}, headers=HEADERS
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["pinned"] is True and body["title"] == "新标题"
    assert body["updated_at"] >= body["created_at"]

    listed = (
        await rig.client.get(f"{V1}/workspaces/{body['workspace_id']}/sessions", headers=HEADERS)
    ).json()
    assert listed[0]["session_id"] == sid


@pytest.mark.unit
async def test_prompt_crud_and_system_setting_stay_in_sync(rig: ApiRig) -> None:
    listing = (await rig.client.get(f"{V1}/prompts", headers=HEADERS)).json()
    names = {item["name"] for item in listing}
    assert {"system", "compactor", "subagent"} <= names

    original = (await rig.client.get(f"{V1}/prompts/system", headers=HEADERS)).json()
    assert original["customized"] is False and original["content"] == original["default"]

    changed = await rig.client.put(
        f"{V1}/prompts/system", json={"content": "只回复 OK"}, headers=HEADERS
    )
    assert changed.status_code == 200
    assert changed.json()["content"] == "只回复 OK" and changed.json()["default"] != "只回复 OK"

    settings = (await rig.client.get(f"{V1}/settings", headers=HEADERS)).json()
    assert settings["system_prompt"] == "只回复 OK"
    assert settings["system_prompt_default"] == original["default"]

    reset = await rig.client.delete(f"{V1}/prompts/system", headers=HEADERS)
    assert reset.status_code == 200 and reset.json()["customized"] is False
    again = (await rig.client.get(f"{V1}/settings", headers=HEADERS)).json()
    assert again["system_prompt"] is None


@pytest.mark.unit
async def test_workspace_settings(rig: ApiRig) -> None:
    wid = await rig.create_workspace()
    rig.services.introspection.models = lambda: [{"name": "deepseek-flash"}]
    first = (await rig.client.get(f"{V1}/workspaces/{wid}/settings", headers=HEADERS)).json()
    assert first["permission_level"] is None and first["defaults"]["permission_level"]

    put = await rig.client.put(
        f"{V1}/workspaces/{wid}/settings",
        json={"permission_level": "read_only", "model": "deepseek-flash", "rules_enabled": True},
        headers=HEADERS,
    )
    assert put.status_code == 200
    assert put.json()["permission_level"] == "read_only" and put.json()["rules_enabled"] is True
    stored = await load_workspace_settings(rig.services.memory, wid)
    assert stored.model == "deepseek-flash"


@pytest.mark.unit
async def test_skill_and_mcp_toggles_persist(rig: ApiRig) -> None:
    async def catalog() -> list[dict[str, Any]]:
        return [
            {
                "name": "builtin_one",
                "source": "builtin",
                "description": "d",
                "triggers": [],
                "required_tools": [],
                "high_privilege": [],
                "warnings": [],
                "enabled": True,
            }
        ]

    rig.services.skill_catalog = catalog
    sources = (await rig.client.get(f"{V1}/skills/sources", headers=HEADERS)).json()
    assert sources[0]["source"] == "builtin" and sources[0]["count"] == 1

    off = await rig.client.put(
        f"{V1}/skills/builtin_one/enabled", json={"enabled": False}, headers=HEADERS
    )
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert "builtin_one" in (await load_skill_policy(rig.services.memory)).disabled

    source = await rig.client.put(
        f"{V1}/skills/sources/workspace", json={"enabled": True}, headers=HEADERS
    )
    assert source.json()["enabled"] is True

    class FakeMcp:
        global_enabled = True
        applied: tuple[Any, Any] | None = None

        def apply_policy(self, *, enabled: Any, servers: Any) -> None:
            self.applied = (enabled, servers)

        def status(self) -> list[dict[str, Any]]:
            return [
                {
                    "name": "srv",
                    "transport": "stdio",
                    "status": "idle",
                    "enabled": True,
                    "exposure": "deferred",
                    "disabled_tools": [],
                    "command_preview": "npx x",
                    "tool_count": 1,
                    "tools": ["t"],
                    "rejected": [],
                    "last_error": "",
                    "retry_in_sec": 0,
                }
            ]

        def server_status(self, name: str) -> dict[str, Any] | None:
            return next((row for row in self.status() if row["name"] == name), None)

        async def reconnect(self, name: str) -> dict[str, Any] | None:
            return self.server_status(name)

    fake = FakeMcp()
    rig.services.mcp_control = fake
    assert (await rig.client.get(f"{V1}/mcp/settings", headers=HEADERS)).json() == {"enabled": True}
    assert (await rig.client.get(f"{V1}/mcp/servers", headers=HEADERS)).json()[0]["name"] == "srv"
    updated = await rig.client.put(
        f"{V1}/mcp/servers/srv",
        json={"enabled": False, "exposure": "pinned"},
        headers=HEADERS,
    )
    assert updated.status_code == 200 and updated.json()["name"] == "srv"
    policy = await load_mcp_policy(rig.services.memory)
    assert policy.server("srv").enabled is False
    assert policy.server("srv").exposure == "pinned"
    assert fake.applied is not None

    master = await rig.client.put(f"{V1}/mcp/settings", json={"enabled": False}, headers=HEADERS)
    assert master.status_code == 200
    assert fake.applied is not None and fake.applied[0] is False


@pytest.mark.unit
async def test_context_segment_route(rig: ApiRig) -> None:
    sid = await rig.create_session()

    async def segment(_: str, name: str) -> dict[str, Any] | None:
        return {"id": name, "text": "段内容", "tokens": 3} if name == "system" else None

    rig.services.context_segment = segment
    ok = await rig.client.get(f"{V1}/sessions/{sid}/context/segments/system", headers=HEADERS)
    assert ok.status_code == 200 and ok.json()["text"] == "段内容"
    missing = await rig.client.get(f"{V1}/sessions/{sid}/context/segments/nope", headers=HEADERS)
    assert missing.status_code == 404
