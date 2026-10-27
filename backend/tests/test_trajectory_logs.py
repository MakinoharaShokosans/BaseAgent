"""轨迹文件与日志。出处：docs/08_observability/02_trajectory_and_logs.md §1、§2。"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from loguru import logger

from agent_runtime.core import current_task_id
from agent_runtime.observability import (
    Event,
    EventHub,
    Redactor,
    TrajectoryRecorder,
    setup_logging,
    to_record,
)

REDACTOR = Redactor(["key", "token", "secret", "password"], [r"sk-[A-Za-z0-9]{16,}"])


def ev(name: str, seq: int = 1, **data: object) -> Event:
    return Event("t1", seq, 100.0 + seq, name, data)


# ------------------------------------------------------------------ 事件到记录


@pytest.mark.unit
def test_every_record_is_versioned_and_tool_args_are_present_with_the_redaction_flag() -> None:
    rec = to_record(
        ev(
            "tool.call",
            tool="bash",
            args={"command": "ls", "api_key": "[REDACTED]"},
            call_id="c1",
            step=2,
        )
    )
    assert rec is not None
    assert (rec["v"], rec["type"], rec["tool_name"], rec["call_id"]) == (2, "tool", "bash", "c1")
    assert rec["tool_args"]["command"] == "ls" and rec["tool_args_redacted"] is True  # 命令不缺失
    clean = to_record(ev("tool.call", tool="bash", args={"command": "ls"}))
    assert clean is not None and clean["tool_args_redacted"] is False


@pytest.mark.unit
def test_record_types_cover_the_contract() -> None:
    cases = {
        "node.finished": ("node", {"node": "planner", "stage": 1, "decision": "x"}),
        "plan": ("node", {"thought": "t"}),
        "tool.result": ("tool", {"tool": "bash", "ok": False, "result": "boom", "call_id": "c1"}),
        "guard.loop_blocked": ("guard", {"call_ids": ["c1"]}),
        "task.waiting_for_approval": ("guard", {"approval_id": "a", "command": "curl x"}),
        "llm.call": (
            "llm",
            {"node": "planner", "tier": "fast", "tokens": 90, "outcome": "ok", "duration_ms": 5},
        ),
        "research.round": ("subagent", {"round": 1}),
        "task.finished": ("final", {"status": "succeeded", "delivery": "done"}),
        "task.error": ("guard", {"code": "TASK_FAILED", "message": "x"}),
    }
    for name, (kind, data) in cases.items():
        record = to_record(ev(name, **data))
        assert record is not None and record["type"] == kind, name
    llm = to_record(
        ev(
            "llm.call",
            node="p",
            tier="fast",
            model="m",
            tokens=7,
            duration_ms=3,
            attempts=2,
            outcome="error",
            error="Timeout",
        )
    )
    assert (
        llm is not None
        and llm["llm"]["usage"]["total_tokens"] == 7
        and llm["ok"] is False
        and llm["error_class"] == "Timeout"
    )


@pytest.mark.unit
def test_transport_events_are_not_trajectory() -> None:
    for name in ("heartbeat", "stream.dropped", "task.started", "something.unknown"):
        assert to_record(ev(name)) is None


@pytest.mark.unit
def test_long_text_is_summarised_not_copied_whole() -> None:
    rec = to_record(ev("tool.result", tool="bash", result="x" * 5000, ok=True))
    assert rec is not None and len(rec["observation_summary"]) <= 500


# ------------------------------------------------------------------ 写与读


@pytest.mark.unit
def test_records_are_appended_one_line_each_flushed_and_private(tmp_path: Path) -> None:
    rec = TrajectoryRecorder(tmp_path / "traces")
    rec.record(ev("tool.call", 1, tool="bash", args={"command": "ls"}))
    rec.record(ev("tool.result", 2, tool="bash", ok=True, result="a"))
    path = rec.path_of("t1")
    lines = path.read_text().splitlines()  # 还没关闭任何句柄就已经能读到：立即落盘
    assert [json.loads(line)["seq"] for line in lines] == [1, 2]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.unit
def test_reading_is_paged_and_tolerates_a_torn_last_line(tmp_path: Path) -> None:
    rec = TrajectoryRecorder(tmp_path)
    for i in range(1, 6):
        rec.record(ev("tool.result", i, tool="t", ok=True, result=str(i)))
    with rec.path_of("t1").open("a") as handle:
        handle.write('{"seq": 6, "v"')  # 强杀时写了一半
    assert [r["seq"] for r in rec.read("t1", offset=1, limit=2)] == [2, 3]
    assert [r["seq"] for r in rec.read("t1")] == [1, 2, 3, 4, 5]
    assert rec.read("nope") == []


@pytest.mark.security
@pytest.mark.parametrize("task_id", ["../x", "a/b", "", "x" * 65, "..", ".hidden", "a b"])
def test_task_ids_cannot_escape_the_trace_root(tmp_path: Path, task_id: str) -> None:
    rec = TrajectoryRecorder(tmp_path / "traces")
    with pytest.raises(ValueError):
        rec.path_of(task_id)
    rec.record(
        Event(task_id, 1, 1.0, "tool.call", {"tool": "x", "args": {}})
    )  # 写入失败也只告警，不抛
    assert not list(tmp_path.rglob("*.jsonl"))


@pytest.mark.unit
def test_write_failures_never_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    TrajectoryRecorder(blocker / "traces").record(ev("tool.call", tool="x", args={}))


@pytest.mark.unit
def test_delete_removes_the_file(tmp_path: Path) -> None:
    rec = TrajectoryRecorder(tmp_path)
    rec.record(ev("tool.call", tool="x", args={}))
    assert rec.exists("t1")
    rec.delete("t1")
    rec.delete("t1")
    assert not rec.exists("t1")


@pytest.mark.integration
async def test_the_hub_feeds_the_recorder_and_a_failing_sink_does_not_stop_events(
    tmp_path: Path,
) -> None:
    hub = EventHub()
    rec = TrajectoryRecorder(tmp_path)

    def broken(event: Event) -> None:
        raise RuntimeError("sink down")

    hub.add_sink(broken)
    hub.add_sink(rec.record)
    await hub.emit("t1", "tool.call", {"tool": "bash", "args": {"command": "ls"}})
    await hub.emit("t1", "task.finished", {"status": "succeeded"})
    assert [r["type"] for r in rec.read("t1")] == ["tool", "final"]
    assert [e.seq for e in await hub.history("t1")] == [1, 2]


# ------------------------------------------------------------------ 日志


@pytest.fixture
def log_file(tmp_path: Path):  # type: ignore[no-untyped-def]
    path = tmp_path / "logs" / "baseagent.jsonl"
    setup_logging(
        level="DEBUG", console=False, file=path, rotation_mb=1, retention_files=2, redactor=REDACTOR
    )
    yield path
    logger.remove()


def lines(path: Path) -> list[dict]:  # type: ignore[type-arg]
    logger.complete()
    return [json.loads(x) for x in path.read_text().splitlines() if x]


@pytest.mark.unit
def test_the_task_context_is_injected_automatically_and_secrets_are_redacted(
    log_file: Path,
) -> None:
    current_task_id.set("task_42")
    logger.info("calling with token sk-abcdefghijklmnop1234 and API_KEY=hunter22")
    logger.bind(password="p@ss").warning("with extra")
    current_task_id.set(None)
    records = lines(log_file)
    first = records[0]["record"]
    assert first["extra"]["task_id"] == "task_42"
    assert "sk-abcdefghijklmnop1234" not in first["message"] and "hunter22" not in first["message"]
    assert records[1]["record"]["extra"]["password"] == "[REDACTED]"


@pytest.mark.unit
def test_no_context_means_no_task_id(log_file: Path) -> None:
    current_task_id.set(None)
    logger.info("plain")
    assert "task_id" not in lines(log_file)[0]["record"]["extra"]


@pytest.mark.security
def test_exceptions_do_not_dump_local_variables_and_the_file_is_private(log_file: Path) -> None:
    def fail() -> None:
        secret_local = "SUPER-SECRET-LOCAL-VALUE"
        raise RuntimeError(len(secret_local))

    try:
        fail()
    except RuntimeError:
        logger.exception("boom")
    text = log_file.read_text()
    assert "SUPER-SECRET-LOCAL-VALUE" not in text  # diagnose=False
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_file.parent.stat().st_mode) == 0o700


@pytest.mark.unit
def test_setup_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    for _ in range(3):
        setup_logging(
            level="INFO",
            console=False,
            file=path,
            rotation_mb=1,
            retention_files=1,
            redactor=REDACTOR,
        )
    logger.info("once")
    logger.complete()
    assert len(path.read_text().splitlines()) == 1
    logger.remove()
    _ = os
