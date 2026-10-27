"""文件系统相关的边界：工作区根路径校验、工作区内路径、目录浏览。

设计文档：docs/09_api/01_endpoints.md §1、§5，03 §4。纯同步函数，由路由放到线程里跑。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from tools.builtin.paths import PathEscapeError, resolve_within_root

_NOISE_DIRS: Final = frozenset(
    {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
)
_TREE_MAX_DEPTH: Final = 8
_TREE_MAX_ENTRIES: Final = 5000


class WorkspacePathError(ValueError):
    """工作区根路径不合规（不存在、不是目录、过宽）。"""


def validate_workspace_root(
    raw: str, *, forbidden: Collection[str], forbid_home: bool, home: Path | None = None
) -> Path:
    """存在且是目录；解析符号链接后保存；拒绝根目录、系统目录与用户主目录本身。"""
    if "\x00" in raw:
        raise WorkspacePathError("路径含有非法字符")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise WorkspacePathError("工作区路径必须是绝对路径")
    if not path.exists():
        raise WorkspacePathError("路径不存在")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise WorkspacePathError("路径不是目录")
    for banned in forbidden:
        bare = banned.rstrip("/")
        if banned == "/" or not bare:
            if resolved == Path("/"):
                raise WorkspacePathError("不能把根目录当作工作区")
            continue
        root = Path(bare).resolve()
        if resolved == root or resolved.is_relative_to(root):
            raise WorkspacePathError(f"路径落在受保护的系统目录 {bare} 之内")
    if forbid_home and resolved == (home or Path.home()).resolve():
        raise WorkspacePathError("不能把用户主目录本身当作工作区")
    return resolved


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def within_workspace(root: Path, relative: str) -> Path:
    """解析工作区内的相对路径；越界（含符号链接）抛 ``PathEscapeError``。"""
    return resolve_within_root(root, relative)


def read_workspace_file(root: Path, relative: str, max_bytes: int) -> tuple[str, str, int]:
    target = within_workspace(root, relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    size = target.stat().st_size
    if size > max_bytes:
        raise FileTooLargeError(size, max_bytes)
    data = target.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotTextFileError(relative) from exc
    return text, hash_bytes(data), size


class FileTooLargeError(ValueError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"文件 {size} 字节，超过上限 {limit}")


class NotTextFileError(ValueError):
    pass


class FileHashConflictError(RuntimeError):
    """文件在读取之后被别处（例如 Agent）改过。"""


@dataclass(frozen=True, slots=True)
class Saved:
    hash: str
    size: int


def save_workspace_file(
    root: Path, relative: str, content: str, *, base_hash: str | None, max_bytes: int
) -> Saved:
    """原子写（同目录临时文件加替换），带 ``base_hash`` 冲突检测。"""
    data = content.encode("utf-8")
    if len(data) > max_bytes:
        raise FileTooLargeError(len(data), max_bytes)
    target = within_workspace(root, relative)
    if target.is_dir():
        raise PathEscapeError(f"{relative} 是目录")
    if base_hash is not None:
        current = hash_bytes(target.read_bytes()) if target.is_file() else None
        if current != base_hash:
            raise FileHashConflictError(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".aegis-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Saved(hash_bytes(data), len(data))


def build_tree(root: Path, ignored: Collection[str]) -> dict[str, object]:
    skip = set(ignored) | _NOISE_DIRS
    counter = {"n": 0}

    def walk(directory: Path, depth: int) -> list[dict[str, object]]:
        nodes: list[dict[str, object]] = []
        try:
            entries = sorted(os.scandir(directory), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return nodes
        for entry in entries:
            if entry.name.startswith(".") and entry.name not in {".env.example"}:
                continue
            if counter["n"] >= _TREE_MAX_ENTRIES:
                break
            is_dir = entry.is_dir(follow_symlinks=False)
            if is_dir and entry.name in skip:
                continue
            if entry.is_symlink() and not _inside(root, Path(entry.path)):
                continue
            counter["n"] += 1
            node: dict[str, object] = {
                "name": entry.name,
                "path": str(Path(entry.path).relative_to(root)),
                "is_dir": is_dir,
            }
            if is_dir:
                node["children"] = (
                    walk(Path(entry.path), depth + 1) if depth < _TREE_MAX_DEPTH else []
                )
            nodes.append(node)
        return nodes

    return {"name": root.name, "path": "", "is_dir": True, "children": walk(root, 1)}


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


# ------------------------------------------------------------------ 目录浏览


class BrowseDeniedError(PathEscapeError):
    pass


def list_directories(
    raw: str | None, *, roots: Collection[str], forbidden: Collection[str], max_entries: int
) -> dict[str, object]:
    """只浏览配置的根之内的目录；不返回隐藏项与受保护的系统目录；条数有上限。"""
    allowed = [Path(r).expanduser().resolve() for r in roots]
    banned = [Path(b).resolve() for b in forbidden if b.rstrip("/")]
    target = (Path(raw).expanduser() if raw else allowed[0]).resolve()
    if not any(target == a or target.is_relative_to(a) for a in allowed):
        raise BrowseDeniedError("目录不在允许浏览的范围内")
    if any(target == b or target.is_relative_to(b) for b in banned):
        raise BrowseDeniedError("目录受保护，不能浏览")
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    entries: list[dict[str, object]] = []
    truncated = False
    for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        child = Path(entry.path)
        entries.append(
            {"name": entry.name, "path": str(child), "has_children": _has_subdirs(child)}
        )
    in_roots = any(target == a for a in allowed)
    return {
        "path": str(target),
        "parent": None if in_roots else str(target.parent),
        "entries": entries,
        "truncated": truncated,
    }


def _has_subdirs(path: Path) -> bool:
    try:
        return any(
            e.is_dir(follow_symlinks=False) and not e.name.startswith(".") for e in os.scandir(path)
        )
    except OSError:
        return False
