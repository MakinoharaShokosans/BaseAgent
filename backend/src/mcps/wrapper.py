"""stdio 服务器的启动包装：在 ``exec`` 目标程序之前施加资源上限并建立独立会话。

用法：``python -m mcps.wrapper <as_mb> <fsize_mb> <cpu_sec> -- <command> [args...]``。
不用 shell 的 ``ulimit``：不同 ``sh`` 的单位不一致，``setrlimit`` 以字节计，没有歧义。
设计文档：docs/04_tools/04_mcp.md §4.2。仅 POSIX。
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Final

_MB: Final = 1024 * 1024


def _limit(which: int, value: int) -> None:
    import resource

    _, hard = resource.getrlimit(which)
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    resource.setrlimit(which, (value, value if hard == resource.RLIM_INFINITY else hard))


def main(argv: list[str]) -> None:
    import resource

    try:
        sep = argv.index("--")
        as_mb, fsize_mb, cpu_sec = (int(x) for x in argv[:sep])
        command = argv[sep + 1 :]
    except (ValueError, TypeError):
        sys.exit("用法：mcps.wrapper <as_mb> <fsize_mb> <cpu_sec> -- <command> [args...]")
    if not command:
        sys.exit("缺少要启动的命令")
    _limit(resource.RLIMIT_AS, as_mb * _MB)
    _limit(resource.RLIMIT_FSIZE, fsize_mb * _MB)
    _limit(resource.RLIMIT_CPU, cpu_sec)
    with contextlib.suppress(OSError):  # 已经是会话组长：无妨
        os.setsid()
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
