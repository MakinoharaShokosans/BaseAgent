"""写文件后的即时语法诊断。出处：docs/04_tools/02_builtin_tools.md（write_file / edit_file）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.builtin import EditFileTool, FileLimits, WriteFileTool
from tools.builtin.diagnostics import check_syntax

LIMITS = FileLimits(view_max_lines=50, view_max_file_bytes=100_000, write_max_bytes=100_000)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("a.py", "def f(:\n    pass\n"),
        ("a.json", '{"a": 1,}'),
        ("a.toml", "x = = 1"),
    ],
)
def test_broken_files_are_diagnosed_with_a_location(path: str, content: str) -> None:
    problem = check_syntax(path, content)
    assert problem is not None and len(problem) <= 200


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("a.py", "def f():\n    return 1\n"),
        ("a.json", '{"a": [1, 2]}'),
        ("a.toml", "x = 1\n[t]\ny = 'z'\n"),
        ("notes.md", "def f(:  这不是代码"),  # 没有对应解析器：不诊断
        ("a.py", "x = 1\n" * 100_000),  # 过大不检查
    ],
)
def test_valid_or_unknown_files_are_not_flagged(path: str, content: str) -> None:
    assert check_syntax(path, content) is None


@pytest.mark.unit
def test_null_bytes_and_deep_nesting_do_not_raise() -> None:
    assert check_syntax("a.py", "x = 1\0") is not None
    check_syntax("a.json", "[" * 100_000)  # 只要不抛


@pytest.mark.unit
async def test_write_file_reports_a_syntax_error_but_still_writes(tmp_path: Path) -> None:
    result = await WriteFileTool(tmp_path, LIMITS).invoke(
        {"path": "bad.py", "content": "def f(:\n    pass\n"}
    )
    assert result.ok and "已写入 bad.py" in result.content
    assert "[语法检查]" in result.content and "第 1 行" in result.content
    assert (tmp_path / "bad.py").read_text() == "def f(:\n    pass\n"


@pytest.mark.unit
async def test_write_file_is_quiet_when_the_file_is_fine(tmp_path: Path) -> None:
    result = await WriteFileTool(tmp_path, LIMITS).invoke(
        {"path": "ok.py", "content": "def f():\n    return 1\n"}
    )
    assert result.ok and "[语法检查]" not in result.content


@pytest.mark.unit
async def test_edit_file_flags_an_edit_that_breaks_the_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    result = await EditFileTool(tmp_path, LIMITS).invoke(
        {"path": "a.py", "old_string": "    return 1\n", "new_string": "    return (1\n"}
    )
    assert result.ok and "[语法检查]" in result.content and "a.py" in result.content
    fixed = await EditFileTool(tmp_path, LIMITS).invoke(
        {"path": "a.py", "old_string": "return (1", "new_string": "return 1"}
    )
    assert fixed.ok and "[语法检查]" not in fixed.content
