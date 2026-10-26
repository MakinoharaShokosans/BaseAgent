"""记忆层的错误。"""

from __future__ import annotations


class MemoryError_(Exception):
    """记忆库操作失败。"""


class WorkspaceNotFound(MemoryError_):
    """工作区不存在。不会悄悄补建一个根路径虚构的默认工作区。"""


class SessionNotFound(MemoryError_):
    pass


class WorkspacePathConflict(MemoryError_):
    """同一路径已有工作区。"""


class WatermarkRegressionError(MemoryError_):
    """水位线只增不减。"""
