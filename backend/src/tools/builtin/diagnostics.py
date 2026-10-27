"""写文件后的即时语法诊断：把“改坏了”的反馈直接放进这次工具的观察值里。

只做进程内、无副作用的解析（``ast`` / ``json`` / ``tomllib``），不启动任何子进程，也不执行内容。
没有对应解析器的扩展名返回 None（不诊断，不代表没问题）。
"""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import PurePosixPath

_MAX_CHARS = 200_000
_MESSAGE_CLIP = 200


def check_syntax(path: str, content: str) -> str | None:
    """内容有语法错误时返回一行诊断，否则 None。过大的内容不检查。"""
    if len(content) > _MAX_CHARS:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    try:
        if suffix in (".py", ".pyi"):
            ast.parse(content, filename=path)
        elif suffix == ".json":
            json.loads(content)
        elif suffix == ".toml":
            tomllib.loads(content)
        else:
            return None
    except SyntaxError as exc:
        where = f"第 {exc.lineno} 行" if exc.lineno else "未知位置"
        return f"{where}：{_clip(exc.msg)}"
    except json.JSONDecodeError as exc:
        return f"第 {exc.lineno} 行：{_clip(exc.msg)}"
    except tomllib.TOMLDecodeError as exc:
        return _clip(str(exc))
    except (ValueError, RecursionError, MemoryError) as exc:  # 例如源码含空字节、嵌套过深
        return _clip(f"{type(exc).__name__}: {exc}")
    return None


def _clip(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= _MESSAGE_CLIP else flat[: _MESSAGE_CLIP - 1] + "…"
