"""用户设置：存储、接口校验、对会话压缩与模型调用的作用。出处：docs/09_api/01_endpoints.md §2.1。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from agent_runtime.api.deps import Introspection
from agent_runtime.core import Message
from agent_runtime.llm import LLMRequest, PreferredEndpoint
from agent_runtime.memory import (
    CompactionConfig,
    MemoryStore,
    MergeLimits,
    SessionCompactor,
    SessionNotFound,
    SessionPreferences,
)
from api_support import HEADERS, ApiRig, build_rig

V1 = "/api/v1"
MODELS = [
    {"tier": "reasoning", "name": "big", "model": "m-big", "context_window": 200_000},
    {"tier": "fast", "name": "small", "model": "m-small", "context_window": 64_000},
    {"tier": "reasoning", "name": "small", "model": "m-small", "context_window": 64_000},
]


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    async for r in build_rig(tmp_path):
        r.services.introspection = Introspection(models=lambda: [dict(m) for m in MODELS])
        yield r


async def new_session(rig: ApiRig) -> str:
    ws = await rig.client.post(
        f"{V1}/workspaces", json={"name": "w", "root_path": str(rig.root)}, headers=HEADERS
    )
    s = await rig.client.post(
        f"{V1}/workspaces/{ws.json()['workspace_id']}/sessions", headers=HEADERS
    )
    return str(s.json()["session_id"])


# ==============================================================================
# 存储
# ==============================================================================


@pytest.fixture
async def store(tmp_path: Path):  # type: ignore[no-untyped-def]
    async with MemoryStore(tmp_path / "m.db") as s:
        await s.create_workspace("w", "w", str(tmp_path))
        await s.create_session("s", "w")
        yield s


@pytest.mark.unit
async def test_session_settings_round_trip_and_none_means_default(store: MemoryStore) -> None:
    assert await store.get_session_settings("s") == {}
    await store.set_session_settings("s", {"model": "big", "context_token_limit": None})
    assert await store.get_session_settings("s") == {"model": "big"}
    with pytest.raises(SessionNotFound):
        await store.get_session_settings("nope")


@pytest.mark.unit
async def test_session_settings_go_away_with_the_session(store: MemoryStore) -> None:
    await store.set_session_settings("s", {"model": "big"})
    await store.delete_session("s")
    await store.create_session("s", "w")
    assert await store.get_session_settings("s") == {}


@pytest.mark.unit
async def test_app_setting_set_and_clear(store: MemoryStore) -> None:
    assert await store.get_app_setting("system_prompt") is None
    await store.set_app_setting("system_prompt", "x")
    await store.set_app_setting("system_prompt", "y")
    assert await store.get_app_setting("system_prompt") == "y"
    await store.set_app_setting("system_prompt", None)
    assert await store.get_app_setting("system_prompt") is None


@pytest.mark.unit
def test_preferences_tolerate_bad_stored_values() -> None:
    prefs = SessionPreferences.from_mapping({"model": 3, "context_token_limit": 10})
    assert prefs == SessionPreferences()
    assert (
        SessionPreferences.from_mapping({"context_token_limit": True}).context_token_limit is None
    )


# ==============================================================================
# 作用：会话压缩阈值
# ==============================================================================


class _Extractor:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: Any) -> None:
        self.calls += 1
        return None


@pytest.mark.unit
async def test_a_session_context_limit_overrides_the_compaction_threshold(
    store: MemoryStore,
) -> None:
    for i in range(4):
        await store.append_turn("s", f"t{i}", "user", "q", 1000)
        await store.append_turn("s", f"t{i}", "assistant", "a", 1000)
    config = CompactionConfig(
        token_limit=4000,
        high_watermark=0.8,
        compaction_ratio=0.4,
        keep_recent_turns=1,
        backoff_sec=(30,),
        limits=MergeLimits(
            max_facts=10, max_failures=10, summary_max_chars=100, require_provenance=True
        ),
    )
    ex = _Extractor()
    compactor = SessionCompactor(store, ex, config, clock=lambda: 0.0)
    await store.set_session_settings("s", {"context_token_limit": 20_000})
    assert (await compactor.maybe_compact("s")).status == "below_threshold"  # 8000 < 16000
    await store.set_session_settings("s", {})
    await compactor.maybe_compact("s")  # 8000 ≥ 3200：回到配置值就会触发
    assert ex.calls == 1


# ==============================================================================
# 作用：首选端点
# ==============================================================================


@dataclass
class _Gateway:
    seen: list[str | None] = field(default_factory=list)

    async def invoke(self, request: LLMRequest) -> Any:
        self.seen.append(request.prefer_endpoint)
        return None

    async def invoke_structured(self, request: LLMRequest, model: type[BaseModel]) -> Any:
        self.seen.append(request.prefer_endpoint)
        return None


@pytest.mark.unit
async def test_preferred_endpoint_fills_in_but_never_overrides_an_explicit_choice() -> None:
    inner = _Gateway()
    gw = PreferredEndpoint(inner, "big")
    msg = [Message(role="user", content="hi")]
    await gw.invoke(LLMRequest(tier="fast", messages=msg))
    await gw.invoke_structured(LLMRequest(tier="fast", messages=msg), BaseModel)
    await gw.invoke(LLMRequest(tier="fast", messages=msg, prefer_endpoint="other"))
    assert inner.seen == ["big", "big", "other"]


# ==============================================================================
# 接口
# ==============================================================================


@pytest.mark.unit
async def test_session_settings_defaults_and_bounds_come_from_config(rig: ApiRig) -> None:
    sid = await new_session(rig)
    data = (await rig.client.get(f"{V1}/sessions/{sid}/settings", headers=HEADERS)).json()
    assert data["model"] is None and data["context_token_limit"] is None
    assert (
        data["defaults"]["context_token_limit"] == rig.services.settings.context.session.token_limit
    )
    assert data["bounds"]["context_token_limit"] == {"min": 4000, "max": 64_000, "step": 1000}


@pytest.mark.unit
async def test_put_session_settings_validates_model_and_limit(rig: ApiRig) -> None:
    sid = await new_session(rig)
    url = f"{V1}/sessions/{sid}/settings"
    ok = await rig.client.put(
        url, json={"model": "big", "context_token_limit": 50_000}, headers=HEADERS
    )
    assert ok.status_code == 200 and ok.json()["model"] == "big"
    assert (await rig.client.get(url, headers=HEADERS)).json()["context_token_limit"] == 50_000

    unknown = await rig.client.put(url, json={"model": "ghost"}, headers=HEADERS)
    assert unknown.status_code == 422
    too_big = await rig.client.put(url, json={"context_token_limit": 64_001}, headers=HEADERS)
    assert too_big.status_code == 422

    reset = await rig.client.put(url, json={}, headers=HEADERS)
    assert reset.json()["model"] is None and reset.json()["context_token_limit"] is None
    missing = await rig.client.get(f"{V1}/sessions/nope/settings", headers=HEADERS)
    assert missing.status_code == 404


@pytest.mark.unit
async def test_system_prompt_override_and_restore(rig: ApiRig) -> None:
    first = (await rig.client.get(f"{V1}/settings", headers=HEADERS)).json()
    assert first["system_prompt"] is None and first["system_prompt_default"]
    put = await rig.client.put(
        f"{V1}/settings", json={"system_prompt": "  自定义  "}, headers=HEADERS
    )
    assert put.json()["system_prompt"] == "自定义"
    blank = await rig.client.put(f"{V1}/settings", json={"system_prompt": "   "}, headers=HEADERS)
    assert blank.json()["system_prompt"] is None
    huge = await rig.client.put(
        f"{V1}/settings", json={"system_prompt": "x" * 20_001}, headers=HEADERS
    )
    assert huge.status_code == 422
    assert (await rig.client.get(f"{V1}/settings")).status_code == 401
