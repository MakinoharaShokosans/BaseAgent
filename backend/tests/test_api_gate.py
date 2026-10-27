"""接入闸门：Host、Origin、令牌、限速、请求体大小、trace id、错误形状。

出处：docs/09_api/03_access_and_errors.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runtime.api import FailureLimiter, provision_token, tokens_match
from api_support import HEADERS, TOKEN, ApiRig, build_rig

URL = "/api/v1/workspaces"


@pytest.fixture
async def rig(tmp_path: Path):  # type: ignore[no-untyped-def]
    async for r in build_rig(tmp_path):
        yield r


def error_of(response) -> dict:  # type: ignore[no-untyped-def]
    return response.json()["error"]


# ------------------------------------------------------------------ 令牌


@pytest.mark.security
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": "Basic x"}],
)
async def test_missing_or_wrong_credentials_are_401_and_indistinguishable(
    rig: ApiRig, headers: dict[str, str]
) -> None:
    response = await rig.client.get(URL, headers=headers)
    assert response.status_code == 401
    err = error_of(response)
    assert err["code"] == "UNAUTHORIZED" and err["details"] == {}
    assert TOKEN not in response.text and "wrong" not in response.text


@pytest.mark.security
async def test_the_right_token_passes(rig: ApiRig) -> None:
    assert (await rig.client.get(URL, headers=HEADERS)).status_code == 200


@pytest.mark.security
@pytest.mark.parametrize("path", ["/", "/health", "/api/v1/health", "/docs", "/openapi.json"])
async def test_exempt_paths_need_no_token(rig: ApiRig, path: str) -> None:
    assert (await rig.client.get(path)).status_code == 200


@pytest.mark.security
async def test_repeated_failures_get_rate_limited_even_for_the_right_token_afterwards(
    tmp_path: Path,
) -> None:
    async for rig in build_rig(tmp_path / "a"):
        for _ in range(10):
            assert (await rig.client.get(URL)).status_code == 401
        blocked = await rig.client.get(URL, headers=HEADERS)
        assert blocked.status_code == 429 and error_of(blocked)["code"] == "AUTH_RATE_LIMITED"


@pytest.mark.unit
async def test_auth_can_be_disabled_by_having_no_token(tmp_path: Path) -> None:
    async for rig in build_rig(tmp_path, token=None):
        assert (await rig.client.get(URL)).status_code == 200


@pytest.mark.unit
def test_token_comparison_and_limiter_window() -> None:
    assert tokens_match("abc", "abc") and not tokens_match("abc", "abd")
    assert not tokens_match("abc", "abcd") and not tokens_match("abc", "")
    now = {"t": 0.0}
    limiter = FailureLimiter(2, 10, clock=lambda: now["t"])
    limiter.record_failure("a")
    assert not limiter.blocked("a")
    limiter.record_failure("a")
    assert limiter.blocked("a") and not limiter.blocked("b")
    now["t"] = 11.0
    assert not limiter.blocked("a")  # 窗口滑出


@pytest.mark.security
def test_token_file_is_created_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "api_token"
    first = provision_token(path)
    assert len(first) >= 40 and path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert provision_token(path) == first
    path.unlink()
    assert provision_token(path) != first  # 删除文件并重启即轮换


@pytest.mark.security
def test_environment_token_wins_over_the_file(tmp_path: Path) -> None:
    got = provision_token(
        tmp_path / "t", env={"BASEAGENT_API_TOKEN": " fromenv "}, env_name="BASEAGENT_API_TOKEN"
    )
    assert got == "fromenv" and not (tmp_path / "t").exists()


# ------------------------------------------------------------------ Host 与 Origin


@pytest.mark.security
@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8000", "10.0.0.5"])
async def test_foreign_host_is_refused_even_on_exempt_paths(rig: ApiRig, host: str) -> None:
    for path in (URL, "/health"):
        response = await rig.client.get(path, headers={**HEADERS, "Host": host})
        assert response.status_code == 403 and error_of(response)["code"] == "HOST_NOT_ALLOWED"


@pytest.mark.security
@pytest.mark.parametrize("host", ["localhost", "localhost:5173", "127.0.0.1:9", "[::1]:8000"])
async def test_loopback_hosts_are_allowed(rig: ApiRig, host: str) -> None:
    assert (await rig.client.get(URL, headers={**HEADERS, "Host": host})).status_code == 200


@pytest.mark.security
async def test_configured_extra_hosts_are_allowed(tmp_path: Path) -> None:
    async for rig in build_rig(tmp_path, allowed_hosts=["baseagent.local"]):
        ok = await rig.client.get(URL, headers={**HEADERS, "Host": "baseagent.local"})
        assert ok.status_code == 200


@pytest.mark.security
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_cross_site_writes_are_refused(rig: ApiRig, method: str) -> None:
    response = await rig.client.request(
        method, URL, headers={**HEADERS, "Origin": "https://evil.example.com"}, json={}
    )
    assert response.status_code == 403 and error_of(response)["code"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.security
async def test_allowed_origin_writes_pass_and_reads_ignore_origin(rig: ApiRig) -> None:
    allowed = {**HEADERS, "Origin": "http://localhost:5173"}
    r = await rig.client.post(URL, headers=allowed, json={"name": "p", "root_path": str(rig.root)})
    assert r.status_code in (200, 201)
    read = await rig.client.get(URL, headers={**HEADERS, "Origin": "https://evil.example.com"})
    assert read.status_code == 200  # 读请求不做 Origin 校验（所有 GET 都必须只读）


@pytest.mark.security
async def test_preflight_needs_no_token_and_only_whitelisted_origins_get_cors_headers(
    rig: ApiRig,
) -> None:
    good = await rig.client.options(
        URL,
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert good.status_code == 200
    assert good.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in good.headers
    bad = await rig.client.options(
        URL, headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in bad.headers


# ------------------------------------------------------------------ 请求体大小


@pytest.mark.unit
async def test_oversized_bodies_are_413_by_declared_length_and_by_stream(tmp_path: Path) -> None:
    async for rig in build_rig(tmp_path, request_max_bytes=1000):
        big = {"name": "p", "root_path": str(rig.root), "description": "x" * 1500}
        response = await rig.client.post(URL, headers=HEADERS, json=big)
        assert response.status_code == 413 and error_of(response)["code"] == "PAYLOAD_TOO_LARGE"

        async def chunks():  # type: ignore[no-untyped-def]
            for _ in range(5):
                yield b"x" * 400

        streamed = await rig.client.post(URL, headers=HEADERS, content=chunks())
        assert streamed.status_code == 413


# ------------------------------------------------------------------ trace id 与错误形状


@pytest.mark.unit
async def test_trace_id_is_generated_echoed_and_present_in_error_bodies(rig: ApiRig) -> None:
    ok = await rig.client.get("/health")
    generated = ok.headers["x-trace-id"]
    assert len(generated) >= 16
    missing = await rig.client.get("/api/v1/workspaces/ws_nope", headers=HEADERS)
    assert missing.status_code == 404
    err = error_of(missing)
    assert err["code"] == "WORKSPACE_NOT_FOUND"
    assert err["trace_id"] == missing.headers["x-trace-id"]
    given = await rig.client.get(
        "/api/v1/workspaces/ws_nope", headers={**HEADERS, "X-Trace-ID": "client-123"}
    )
    assert (
        given.headers["x-trace-id"] == "client-123" and error_of(given)["trace_id"] == "client-123"
    )


@pytest.mark.security
async def test_hostile_trace_ids_are_replaced(rig: ApiRig) -> None:
    response = await rig.client.get("/health", headers={"X-Trace-ID": "a b\tc"})
    assert response.headers["x-trace-id"] != "a b\tc"
    response = await rig.client.get("/health", headers={"X-Trace-ID": "x" * 200})
    assert len(response.headers["x-trace-id"]) <= 64


@pytest.mark.unit
async def test_validation_errors_are_400_without_echoing_the_input(rig: ApiRig) -> None:
    response = await rig.client.post(
        URL, headers=HEADERS, json={"name": "", "root_path": "/x", "bogus": "sk-secret-value-123"}
    )
    assert response.status_code == 400
    err = error_of(response)
    assert err["code"] == "VALIDATION_ERROR" and 1 <= len(err["details"]["errors"]) <= 10
    assert "sk-secret-value-123" not in response.text


@pytest.mark.unit
async def test_unknown_route_and_method_use_the_same_envelope(rig: ApiRig) -> None:
    missing = await rig.client.get("/api/v1/nope", headers=HEADERS)
    assert missing.status_code == 404 and error_of(missing)["code"] == "ROUTE_NOT_FOUND"
    wrong = await rig.client.put("/api/v1/health", headers=HEADERS, json={})
    assert wrong.status_code == 405 and error_of(wrong)["code"] == "METHOD_NOT_ALLOWED"


@pytest.mark.security
async def test_internal_errors_do_not_leak_details(rig: ApiRig) -> None:
    async def boom(*_: object, **__: object) -> list[object]:
        raise RuntimeError("secret path /home/u/.ssh/id_rsa")

    rig.services.memory.list_workspaces = boom  # type: ignore[method-assign]
    response = await rig.client.get(URL, headers=HEADERS)
    assert response.status_code == 500
    assert error_of(response)["code"] == "INTERNAL_ERROR"
    assert ".ssh" not in response.text and "Traceback" not in response.text
    assert error_of(response)["trace_id"] == response.headers["x-trace-id"]


@pytest.mark.security
async def test_path_parameters_reject_unsafe_ids(rig: ApiRig) -> None:
    for bad in ("..%2Fetc", "a b", "x" * 80):
        response = await rig.client.get(f"/api/v1/tasks/{bad}", headers=HEADERS)
        assert response.status_code in (400, 404)
        assert response.status_code != 200
