"""sidecar HTTP 客户端：失败收敛成 DependencyUnavailableError。出处：01_tool_system.md §9。"""

from __future__ import annotations

import json

import httpx
import pytest

from tools.core import DependencyUnavailableError, SidecarClient


def client(handler: httpx.MockTransport | None = None, **kw: float) -> SidecarClient:
    return SidecarClient(
        "http://127.0.0.1:8002", timeout_sec=kw.get("timeout_sec", 5), transport=handler
    )


def json_handler(status: int, body: object) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


@pytest.mark.unit
async def test_a_post_sends_json_and_returns_status_and_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    c = client(httpx.MockTransport(handler))
    reply = await c.post_json("/api/v1/shell/execute", {"command": "ls"})
    assert (reply.status, reply.body) == (200, {"ok": True})
    assert str(seen[0].url) == "http://127.0.0.1:8002/api/v1/shell/execute"
    assert json.loads(seen[0].content) == {"command": "ls"}
    await c.aclose()


@pytest.mark.unit
async def test_a_get_works_too() -> None:
    c = client(json_handler(200, {"status": "ok"}))
    assert (await c.get_json("/api/v1/health")).body == {"status": "ok"}
    await c.aclose()


@pytest.mark.unit
async def test_statuses_outside_the_accepted_set_are_dependency_failures() -> None:
    c = client(json_handler(500, {"detail": "boom"}))
    with pytest.raises(DependencyUnavailableError, match="500"):
        await c.post_json("/x", {})
    await c.aclose()


@pytest.mark.unit
async def test_extra_accepted_statuses_come_back_as_replies_with_their_body() -> None:
    c = client(json_handler(403, {"rule": "RM_RF_ROOT"}))
    reply = await c.post_json("/x", {}, accepted=(200, 403, 422))
    assert reply.status == 403 and reply.body == {"rule": "RM_RF_ROOT"}
    await c.aclose()


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b"[1, 2]"),
        httpx.Response(200, content=b'"text"'),
        httpx.Response(200, content=b""),
    ],
)
async def test_a_body_that_is_not_a_json_object_is_a_dependency_failure(
    response: httpx.Response,
) -> None:
    c = client(httpx.MockTransport(lambda request: response))
    with pytest.raises(DependencyUnavailableError, match="JSON"):
        await c.post_json("/x", {})
    await c.aclose()


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("garbage"),
    ],
)
async def test_network_errors_and_timeouts_are_dependency_failures(error: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    c = client(httpx.MockTransport(handler))
    with pytest.raises(DependencyUnavailableError) as caught:
        await c.post_json("/x", {})
    assert caught.value.__cause__ is error
    await c.aclose()


@pytest.mark.unit
async def test_the_connection_pool_is_created_lazily_and_shared() -> None:
    c = client(json_handler(200, {}))
    assert c.opened is False
    await c.get_json("/a")
    first = c.http
    await c.get_json("/b")
    assert c.opened is True and c.http is first
    await c.aclose()
    assert c.opened is False
    await c.aclose()  # 再关一次不出错


@pytest.mark.unit
async def test_a_closed_client_can_be_used_again() -> None:
    c = client(json_handler(200, {"n": 1}))
    await c.get_json("/a")
    await c.aclose()
    assert (await c.get_json("/a")).body == {"n": 1}
    await c.aclose()


@pytest.mark.unit
async def test_a_per_call_timeout_overrides_the_default() -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={})

    c = client(httpx.MockTransport(handler), timeout_sec=5)
    await c.post_json("/x", {}, timeout_sec=77)
    await c.post_json("/x", {})
    assert seen[0] == {"connect": 77, "read": 77, "write": 77, "pool": 77}
    assert seen[1] == {"connect": 5, "read": 5, "write": 5, "pool": 5}
    await c.aclose()
