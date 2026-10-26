"""启动时的恢复扫描。设计文档：docs/02_runtime/03_checkpoint_and_resume.md §6。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.checkpoint.store import CheckpointStore


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    #: 上次进程在任务运行中退出。不自动续跑：由调用方标记为失败（“进程中断”），等用户显式 resume
    interrupted: tuple[str, ...]
    #: 挂起在审批上。恢复为等待审批，前端重新连上后可继续
    waiting_for_approval: tuple[str, ...]


async def scan_for_recovery(store: CheckpointStore) -> RecoveryReport:
    unfinished = await store.list_unfinished()
    return RecoveryReport(
        interrupted=tuple(u.task_id for u in unfinished if not u.waiting_for_approval),
        waiting_for_approval=tuple(u.task_id for u in unfinished if u.waiting_for_approval),
    )
