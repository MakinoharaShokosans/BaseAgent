"""bash_shell 真实执行、HTTP 契约、与 BashTool 的端到端。出处：05_security/02_shell_sandbox.md。"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from aegis_config.models import BashShellConfig
from services.bash_shell.app import ShellSettings, create_app
from tools.builtin import BashTool
from tools.core import SidecarClient

EXEC = "/api/v1/shell/execute"


def make_app(tmp_path: Path, **overrides: Any):  # type: ignore[no-untyped-def]
    cfg = BashShellConfig(
        **{
            "timeout_sec": 5,
            "max_timeout_sec": 10,
            "sigterm_grace_sec": 1,
            "max_concurrent": 2,
            "memory_pool_mb": 8192,
            "rlimit_as_mb": 4096,
            "distill_head_lines": 3,
            "distill_tail_lines": 3,
            **overrides,
        }
    )
    return create_app(
        ShellSettings(
            cfg,
            artifacts_dir=tmp_path / "artifacts",
            scratch_dir=tmp_path / "scratch",
            source_env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SECRET_TOKEN": "leak-me"},
        )
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
async def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as c:
        yield c


def body(root: Path, command: str, **extra: Any) -> dict[str, Any]:
    return {
        "workspace_id": "w",
        "workspace_root": str(root),
        "task_id": "task_1",
        "step_id": 1,
        "command": command,
        **extra,
    }


async def run(client: httpx.AsyncClient, root: Path, command: str, **extra: Any) -> dict[str, Any]:
    response = await client.post(EXEC, json=body(root, command, **extra))
    assert response.status_code == 200, response.text
    return dict(response.json())


# ==============================================================================
# 执行
# ==============================================================================


@pytest.mark.integration
async def test_success_failure_and_exit_codes(client: httpx.AsyncClient, workspace: Path) -> None:
    ok = await run(client, workspace, "echo hello; echo oops >&2")
    assert (ok["status"], ok["exit_code"]) == ("SUCCESS", 0)
    assert ok["distilled_stdout"] == "hello" and ok["distilled_stderr"] == "oops"
    bad = await run(client, workspace, "exit 3")
    assert (bad["status"], bad["exit_code"]) == ("FAILED", 3)
    assert ok["execution_time_ms"] >= 0 and ok["timed_out"] is False


@pytest.mark.integration
async def test_command_runs_in_the_workspace_with_stdin_closed(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    (workspace / "marker.txt").write_text("m")
    listing = await run(client, workspace, "ls")
    assert "marker.txt" in listing["distilled_stdout"]
    started = time.monotonic()
    waiting = await run(client, workspace, "cat")  # 没有标准输入：立刻读到 EOF 而不是卡住
    assert waiting["status"] == "SUCCESS" and time.monotonic() - started < 3


@pytest.mark.integration
async def test_writes_inside_the_workspace_work(client: httpx.AsyncClient, workspace: Path) -> None:
    await run(client, workspace, "echo data > out.txt")
    assert (workspace / "out.txt").read_text().strip() == "data"


@pytest.mark.integration
async def test_full_output_is_written_to_the_task_log_and_distilled_in_the_reply(
    client: httpx.AsyncClient, workspace: Path, tmp_path: Path
) -> None:
    reply = await run(client, workspace, "for i in $(seq 1 200); do echo line$i; done")
    assert reply["is_truncated"] is True and "省略" in reply["distilled_stdout"]
    log = Path(reply["artifact_path"])
    assert log == tmp_path / "artifacts" / "task_1" / "step_1_bash.log"
    assert len(log.read_text().splitlines()) == 200  # noqa: ASYNC240
    assert "line1" in reply["distilled_stdout"] and "line200" in reply["distilled_stdout"]


@pytest.mark.security
async def test_secrets_in_the_servers_environment_do_not_reach_commands(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    out = await run(client, workspace, "env; echo HOME=$HOME")
    text = out["distilled_stdout"]
    assert "leak-me" not in text and "SECRET_TOKEN" not in text
    log = Path(out["artifact_path"]).read_text()  # noqa: ASYNC240
    assert "leak-me" not in log
    assert "HOME=/" in log and "scratch" in log  # HOME 是任务的临时目录，不是真实 HOME


# ==============================================================================
# 超时与进程组
# ==============================================================================


@pytest.mark.integration
async def test_timeout_kills_the_process_group_including_children(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    pidfile = workspace / "child.pid"
    command = f"(sleep 60 & echo $! > {pidfile}; wait) & wait"
    started = time.monotonic()
    reply = await run(client, workspace, command, timeout_sec=1)
    assert reply["status"] == "TIMEOUT" and reply["timed_out"] is True
    assert time.monotonic() - started < 6
    await asyncio.sleep(0.2)
    child = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # 派生的子进程一并清退


@pytest.mark.integration
async def test_a_process_ignoring_sigterm_is_killed_after_the_grace_period(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    started = time.monotonic()
    reply = await run(
        client, workspace, "trap '' TERM; while true; do sleep 0.1; done", timeout_sec=1
    )
    assert reply["timed_out"] is True and reply["status"] == "TIMEOUT"
    assert time.monotonic() - started < 6


@pytest.mark.integration
async def test_requested_timeout_is_clamped_to_the_maximum(tmp_path: Path, workspace: Path) -> None:
    app = make_app(tmp_path, max_timeout_sec=1, timeout_sec=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as c:
        started = time.monotonic()
        reply = await run(c, workspace, "sleep 30", timeout_sec=500)
        assert reply["timed_out"] is True and time.monotonic() - started < 6


@pytest.mark.integration
async def test_signals_report_negative_exit_codes(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    reply = await run(client, workspace, "kill -9 $$")
    assert reply["status"] == "KILLED" and reply["exit_code"] == -9


@pytest.mark.integration
async def test_file_size_limit_stops_runaway_writes(tmp_path: Path, workspace: Path) -> None:
    app = make_app(tmp_path, rlimit_fsize_mb=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as c:
        reply = await run(c, workspace, "head -c 5000000 /dev/zero > big.bin")
        assert reply["status"] != "SUCCESS"
        assert (workspace / "big.bin").stat().st_size <= 1024 * 1024


# ==============================================================================
# 拒绝与校验
# ==============================================================================


@pytest.mark.security
async def test_audit_rejection_is_403_with_rule_and_reason(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    response = await client.post(EXEC, json=body(workspace, "rm -rf /"))
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert (
        detail["code"] == "AUDIT_REJECTED" and detail["rule"] == "RM_RF_ROOT" and detail["reason"]
    )


@pytest.mark.security
async def test_write_outside_the_workspace_is_422(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    response = await client.post(EXEC, json=body(workspace, "echo x > ../escape.txt"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PATH_ESCAPE_DETECTED"
    declared = await client.post(EXEC, json=body(workspace, "true", write_paths=["/etc/hosts"]))
    assert declared.status_code == 422
    assert not (workspace.parent / "escape.txt").exists()


@pytest.mark.security
@pytest.mark.parametrize("task_id", ["../x", "a/b", ".hidden", ""])
async def test_unsafe_task_ids_cannot_escape_the_log_root(
    client: httpx.AsyncClient, workspace: Path, task_id: str
) -> None:
    payload = {**body(workspace, "echo hi"), "task_id": task_id}
    response = await client.post(EXEC, json=payload)
    assert response.status_code in (400, 422)


@pytest.mark.unit
async def test_invalid_workspace_and_bad_bodies(client: httpx.AsyncClient, tmp_path: Path) -> None:
    missing = await client.post(EXEC, json=body(tmp_path / "nope", "ls"))
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "WORKSPACE_INVALID"
    relative = await client.post(EXEC, json={**body(tmp_path, "ls"), "workspace_root": "rel"})
    assert relative.status_code == 422
    for bad in (
        {},
        {**body(tmp_path, "ls"), "extra": 1},
        {**body(tmp_path, "ls"), "timeout_sec": -1},
    ):
        r = await client.post(EXEC, json=bad)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "VALIDATION_ERROR"


@pytest.mark.security
async def test_cross_site_and_rebinding_requests_are_refused(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    cross = await client.post(
        EXEC, json=body(workspace, "echo hi"), headers={"Origin": "https://evil.example.com"}
    )
    assert cross.status_code == 403
    rebound = await client.post(
        EXEC, json=body(workspace, "echo hi"), headers={"Host": "evil.example.com"}
    )
    assert rebound.status_code == 403


@pytest.mark.unit
async def test_health_reports_quota(client: httpx.AsyncClient) -> None:
    health = (await client.get("/api/v1/health")).json()
    assert health["status"] == "ok" and health["active"] == 0 and health["pool_free_mb"] > 0


@pytest.mark.integration
async def test_a_full_pool_answers_queued_and_recovers(tmp_path: Path, workspace: Path) -> None:
    app = make_app(tmp_path, max_concurrent=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as c:
        slow = asyncio.create_task(run(c, workspace, "sleep 1.5"))
        await asyncio.sleep(0.4)
        queued = await run(c, workspace, "echo second")
        assert queued["status"] == "QUEUED" and queued["estimated_wait_sec"] > 0
        assert (await slow)["status"] == "SUCCESS"
        again = await run(c, workspace, "echo third")
        assert again["status"] == "SUCCESS"  # 配额已释放
        health = (await c.get("/api/v1/health")).json()
        assert health["active"] == 0


@pytest.mark.integration
async def test_quota_is_released_even_when_the_command_fails_to_start(
    tmp_path: Path, workspace: Path
) -> None:
    app = make_app(tmp_path, max_concurrent=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
    ) as c:
        await run(c, workspace, "exit 1")
        await run(c, workspace, "definitely-not-a-command-xyz")
        assert (await run(c, workspace, "echo ok"))["status"] == "SUCCESS"


# ==============================================================================
# 与 BashTool 的端到端
# ==============================================================================


def tool_for(app: Any, workspace: Path) -> BashTool:
    transport = httpx.ASGITransport(app=app)
    client = SidecarClient("http://127.0.0.1:8002", timeout_sec=20, transport=transport)
    return BashTool(
        client,
        workspace_id="w",
        workspace_root=str(workspace),
        task_id="task_1",
        default_timeout_sec=5,
        max_timeout_sec=10,
    )


@pytest.mark.integration
async def test_bash_tool_against_the_real_sidecar(tmp_path: Path, workspace: Path) -> None:
    tool = tool_for(make_app(tmp_path), workspace)
    ok = await tool.invoke({"command": "echo hi"})
    assert ok.ok and "hi" in ok.content and ok.artifact_id == "step_1_bash"
    failed = await tool.invoke({"command": "echo nope >&2; exit 2"})
    assert not ok.failed and failed.failed and failed.exit_code == 2 and "nope" in failed.content


@pytest.mark.security
async def test_policy_rejection_reason_reaches_the_model(tmp_path: Path, workspace: Path) -> None:
    tool = tool_for(make_app(tmp_path), workspace)
    result = await tool.invoke({"command": "rm -rf /"})
    assert result.failed and "RM_RF_ROOT" in result.content and "根目录" in result.content
    escape = await tool.invoke({"command": "echo x > ../out"})
    assert escape.failed and "沙箱拒绝" in escape.content


@pytest.mark.integration
async def test_queued_is_not_a_failure_for_the_tool(tmp_path: Path, workspace: Path) -> None:
    tool = tool_for(make_app(tmp_path, max_concurrent=1), workspace)
    slow = asyncio.create_task(tool.invoke({"command": "sleep 1.5"}))
    await asyncio.sleep(0.4)
    queued = await tool.invoke({"command": "echo hi"})
    assert queued.ok and not queued.failed and "等待队列" in queued.content
    await slow
