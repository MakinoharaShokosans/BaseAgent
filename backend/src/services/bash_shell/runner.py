"""沙箱执行：进程组、资源上限、最小环境、两段式硬杀、流式落盘与蒸馏。

设计文档：docs/05_security/02_shell_sandbox.md §3.4-3.5、§5.1。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import signal
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from services.bash_shell.distill import DistillLimits, StreamDistiller

Status = Literal["SUCCESS", "FAILED", "TIMEOUT", "KILLED"]
_MB: Final = 1024 * 1024
_SHELL: Final = "/bin/bash" if Path("/bin/bash").exists() else None
_SAFE_ID: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


@dataclass(frozen=True, slots=True)
class RunLimits:
    rlimit_as_mb: int
    rlimit_fsize_mb: int
    rlimit_cpu_sec: int
    sigterm_grace_sec: float
    chunk_bytes: int


@dataclass(frozen=True, slots=True)
class RunResult:
    status: Status
    exit_code: int | None
    stdout: str
    stderr: str
    truncated: bool
    artifact_path: str
    elapsed_ms: int
    timed_out: bool


def safe_component(name: str) -> bool:
    """单层安全目录名：非空、不以点开头、只含白名单字符（防止经 task_id 逃出落盘根目录）。"""
    return bool(name) and not name.startswith(".") and all(c in _SAFE_ID for c in name)


def minimal_env(
    allow: list[str], scratch: Path, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """只传白名单变量；HOME 指向任务的临时目录，不用真实 HOME（避免读到用户配置与密钥）。"""
    env_source = os.environ if source is None else source
    env = {k: env_source[k] for k in allow if k in env_source and k != "HOME"}
    env["HOME"] = str(scratch)
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("TERM", "dumb")
    return env


def _preexec(limits: RunLimits) -> None:  # pragma: no cover - 在子进程里执行
    # macOS (Darwin) 的 dyld 拥有几十 GB 的共享缓存映射，
    # 设置常规虚拟内存限制会导致子进程启动直接崩溃
    if sys.platform != "darwin":
        resource.setrlimit(
            resource.RLIMIT_AS, (limits.rlimit_as_mb * _MB, limits.rlimit_as_mb * _MB)
        )
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (limits.rlimit_fsize_mb * _MB, limits.rlimit_fsize_mb * _MB)
    )
    resource.setrlimit(resource.RLIMIT_CPU, (limits.rlimit_cpu_sec, limits.rlimit_cpu_sec))


async def run_command(
    command: str,
    *,
    cwd: Path,
    timeout_sec: float,
    env: Mapping[str, str],
    log_path: Path,
    limits: RunLimits,
    distill: DistillLimits,
) -> RunResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    out, err = StreamDistiller(distill, str(log_path)), StreamDistiller(distill, str(log_path))
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=dict(env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,  # 独立会话与进程组，超时时整组清退
        preexec_fn=lambda: _preexec(limits),
        executable=_SHELL,
    )
    timed_out = False
    with log_path.open("wb") as log:

        async def pump(stream: asyncio.StreamReader | None, sink: StreamDistiller) -> None:
            if stream is None:
                return
            while chunk := await stream.read(limits.chunk_bytes):
                log.write(chunk)
                sink.feed(chunk)

        readers = asyncio.gather(pump(process.stdout, out), pump(process.stderr, err))
        try:
            await asyncio.wait_for(process.wait(), timeout_sec)
        except TimeoutError:
            timed_out = True
            await _kill_group(process, limits.sigterm_grace_sec)
        except BaseException:
            await _kill_group(process, limits.sigterm_grace_sec)
            raise
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(readers, 5)
    code = process.returncode
    failed = code != 0
    out_d, err_d = out.finish(failed=failed), err.finish(failed=failed)
    return RunResult(
        status=_status(code, timed_out),
        exit_code=code,
        stdout=out_d.text,
        stderr=err_d.text,
        truncated=out_d.truncated or err_d.truncated,
        artifact_path=str(log_path),
        elapsed_ms=round((time.monotonic() - started) * 1000),
        timed_out=timed_out,
    )


def _status(code: int | None, timed_out: bool) -> Status:
    if timed_out:
        return "TIMEOUT"
    if code is not None and code < 0:
        return "KILLED"
    return "SUCCESS" if code == 0 else "FAILED"


async def _kill_group(process: asyncio.subprocess.Process, grace_sec: float) -> None:
    """先对整个进程组发 SIGTERM，宽限期后仍未退出则 SIGKILL。"""
    pid = process.pid
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), grace_sec)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
        await process.wait()
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)  # 主进程退了，组里可能还有孤儿
