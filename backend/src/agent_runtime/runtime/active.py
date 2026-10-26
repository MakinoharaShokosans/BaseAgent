"""正在运行的任务登记：同一 ``task_id`` 同一时刻只有一个 ``drive``。"""

from __future__ import annotations


class ActiveTasks:
    """单进程内的占位登记。不是线程安全的：只在事件循环线程里使用。"""

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def try_acquire(self, task_id: str) -> bool:
        if task_id in self._ids:
            return False
        self._ids.add(task_id)
        return True

    def release(self, task_id: str) -> None:
        self._ids.discard(task_id)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._ids
