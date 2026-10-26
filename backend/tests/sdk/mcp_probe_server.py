"""供 SDK 验证用的最小 MCP stdio 服务器（mcp 2.x 的 MCPServer）。"""

import os
import time

from mcp.server.mcpserver import MCPServer

server = MCPServer("probe")


@server.tool()
def pid() -> str:
    """返回服务器进程号"""
    return str(os.getpid())


@server.tool()
def add(a: int, b: int) -> int:
    """加法"""
    return a + b


@server.tool()
def getenv(name: str) -> str:
    """读取服务器进程的环境变量，不存在返回 <unset>"""
    return os.environ.get(name, "<unset>")


@server.tool()
def sleep(seconds: float) -> str:
    """阻塞指定秒数"""
    time.sleep(seconds)
    return "done"


if __name__ == "__main__":
    server.run("stdio")
