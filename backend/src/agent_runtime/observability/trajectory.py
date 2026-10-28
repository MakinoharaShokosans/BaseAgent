"""轨迹：每个任务一个 JSONL，追加写、立即 flush，强杀不丢已完成的步骤。

设计文档：docs/08_observability/02_trajectory_and_logs.md §1。它是**数据契约**（评测直接消费），
带 ``v`` 版本。记录由事件推出（事件已经过统一脱敏，``tool_args`` 因此也是脱敏后的），
所以事件、轨迹、日志用的是同一套脱敏，不会各写各的。写入失败只告警，永不中断任务。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Final

from loguru import logger

from agent_runtime.observability.events import Event, clip
from agent_runtime.observability.redaction import REDACTED

SCHEMA_VERSION: Final = 2
_SAFE_ID: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SUMMARY: Final = 500


class TrajectoryRecorder:
    def __init__(self, root: Path) -> None:
        self._root = root

    # ------------------------------------------------------------------ 路径

    def path_of(self, task_id: str) -> Path:
        """任务 id 只接受安全字符集：读取接口不能逃出轨迹根目录。"""
        if not _SAFE_ID.match(task_id):
            raise ValueError("非法的任务 id")
        path = (self._root / f"{task_id}.jsonl").resolve()
        if path.parent != self._root.resolve():
            raise ValueError("轨迹路径越界")
        return path

    # ------------------------------------------------------------------ 写

    def record(self, event: Event) -> None:
        """作为事件中心的旁路：同步追加一行。任何失败都只告警。"""
        try:
            entry = to_record(event)
            if entry is None:
                return
            path = self.path_of(event.task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        except Exception as exc:
            logger.warning("轨迹写入失败（已忽略）：{}", type(exc).__name__)

    # ------------------------------------------------------------------ 读

    def read(self, task_id: str, offset: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """按行分页读取，不整个读入内存。文件不存在为空。"""
        path = self.path_of(task_id)
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < offset:
                    continue
                if len(out) >= limit:
                    break
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # 强杀时最后一行可能不完整：跳过，不让整份轨迹不可读
        return out

    def exists(self, task_id: str) -> bool:
        return self.path_of(task_id).is_file()

    def delete(self, task_id: str) -> None:
        self.path_of(task_id).unlink(missing_ok=True)


def to_record(event: Event) -> dict[str, Any] | None:
    """事件到轨迹记录。不属于轨迹的事件（心跳、传输类）返回 None。"""
    d = event.data
    base: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "task_id": event.task_id,
        "seq": event.seq,
        "ts": event.ts,
    }
    name = event.name
    if name == "plan":
        return {
            **base,
            "type": "node",
            "node": "planner",
            "thought": clip(str(d.get("thought", "")), _SUMMARY),
        }
    if name == "node.finished":
        return {
            **base,
            "type": "node",
            "node": d.get("node", ""),
            "phase": d.get("node", ""),
            "stage": d.get("stage", 0),
            "decision": d.get("decision", ""),
            "observation_summary": d.get("output_summary", ""),
            "step_count": d.get("step_count", 0),
            "total_tokens": d.get("total_tokens", 0),
        }
    if name == "tool.call":
        args = d.get("args", {})
        return {
            **base,
            "type": "tool",
            "tool_name": d.get("tool", ""),
            "tool_args": args,
            "tool_args_redacted": REDACTED in json.dumps(args, ensure_ascii=False, default=str),
            "call_id": d.get("call_id", ""),
            "step_count": d.get("step", 0),
        }
    if name == "tool.result":
        return {
            **base,
            "type": "tool",
            "tool_name": d.get("tool", ""),
            "call_id": d.get("call_id", ""),
            "ok": bool(d.get("ok", True)),
            "observation_summary": clip(str(d.get("result", "")), _SUMMARY),
            "artifact_id": d.get("artifact_id", ""),
            "step_count": d.get("step", 0),
        }
    if name.startswith("guard.") or name in {
        "task.waiting_for_approval",
        "task.approved",
        "task.rejected",
    }:
        return {
            **base,
            "type": "guard",
            "event": name,
            **{k: v for k, v in d.items() if k != "call_ids"},
        }
    if name == "llm.call":
        return {
            **base,
            "type": "llm",
            "node": d.get("node", ""),
            "llm": {
                "tier": d.get("tier", ""),
                "endpoint": d.get("endpoint", ""),
                "model": d.get("model", ""),
                "usage": {"total_tokens": d.get("tokens", 0)},
                "duration_ms": d.get("duration_ms", 0),
                "attempts": d.get("attempts", 1),
            },
            "ok": d.get("outcome") == "ok",
            "error_class": d.get("error", ""),
        }
    if name.startswith(("websearch.", "subagent.")):
        return {**base, "type": "subagent", "event": name, **d}
    if name == "task.finished":
        return {
            **base,
            "type": "final",
            "status": d.get("status", ""),
            "termination_reason": d.get("termination_reason", ""),
            "observation_summary": clip(str(d.get("delivery", "")), _SUMMARY),
            "step_count": d.get("step_count", 0),
            "total_tokens": d.get("total_tokens", 0),
        }
    if name == "task.error":
        return {
            **base,
            "type": "guard",
            "event": name,
            "code": d.get("code", ""),
            "message": d.get("message", ""),
        }
    return None
