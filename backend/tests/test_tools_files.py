"""view_file / write_file 与路径沙箱。出处：02_builtin_tools.md §2、§3，01_tool_system.md §11。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from tools.builtin import (
    FileLimits,
    PathEscapeError,
    ViewFileTool,
    WriteFileTool,
    resolve_within_root,
)


def limits(**kw: int) -> FileLimits:
    base = {"view_max_lines": 5, "view_max_file_bytes": 1000, "write_max_bytes": 50}
    return FileLimits(**{**base, **kw})


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "ws").mkdir()
    return (tmp_path / "ws").resolve()


def put(root: Path, rel: str, text: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


# ==============================================================================
# 路径沙箱
# ==============================================================================


@pytest.mark.unit
def test_paths_inside_the_root_resolve_and_the_root_itself_is_allowed(root: Path) -> None:
    put(root, "a/b.txt", "x")
    assert resolve_within_root(root, "a/b.txt") == root / "a" / "b.txt"
    assert resolve_within_root(root, str(root / "a" / "b.txt")) == root / "a" / "b.txt"
    assert resolve_within_root(root, "a/../a/b.txt") == root / "a" / "b.txt"
    assert resolve_within_root(root, ".") == root
    assert resolve_within_root(root, "not/yet/there.txt") == root / "not" / "yet" / "there.txt"


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["../x", "a/../../x", "/etc/passwd", "../ws-evil/x", "a/../.."])
def test_dotdot_and_absolute_escapes_are_refused(root: Path, raw: str) -> None:
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, raw)


@pytest.mark.unit
def test_a_sibling_directory_sharing_the_root_prefix_is_not_inside(root: Path) -> None:
    evil = root.parent / (root.name + "-evil")
    evil.mkdir()
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, str(evil / "x"))


@pytest.mark.unit
def test_a_symlink_pointing_outside_is_an_escape_but_one_pointing_inside_is_fine(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    os.symlink(outside, root / "link_out")
    put(root, "real/f.txt", "x")
    os.symlink(root / "real", root / "link_in")

    with pytest.raises(PathEscapeError):
        resolve_within_root(root, "link_out/secret")
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, "link_out/new_file")  # 目标还不存在也一样
    assert resolve_within_root(root, "link_in/f.txt") == root / "real" / "f.txt"


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["", "   ", "a\x00b"])
def test_empty_and_nul_paths_are_invalid(root: Path, raw: str) -> None:
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, raw)


# ==============================================================================
# view_file
# ==============================================================================


@pytest.mark.unit
async def test_view_returns_numbered_lines_and_declares_itself_idempotent(root: Path) -> None:
    put(root, "a.txt", "alpha\nbeta\ngamma\n")
    tool = ViewFileTool(root, limits())
    assert tool.idempotent is True and tool.trust == "trusted"
    out = await tool.invoke({"path": "a.txt"})
    assert out.ok
    assert out.content.splitlines() == ["     1\talpha", "     2\tbeta", "     3\tgamma"]


@pytest.mark.unit
async def test_a_line_range_is_inclusive_and_one_based(root: Path) -> None:
    put(root, "a.txt", "".join(f"l{i}\n" for i in range(1, 11)))
    out = await ViewFileTool(root, limits()).invoke(
        {"path": "a.txt", "start_line": 3, "end_line": 4}
    )
    assert [ln.split("\t")[1] for ln in out.content.splitlines()] == ["l3", "l4"]


@pytest.mark.unit
async def test_more_than_the_cap_is_cut_with_a_hint_to_continue(root: Path) -> None:
    put(root, "a.txt", "".join(f"l{i}\n" for i in range(1, 21)))
    out = await ViewFileTool(root, limits()).invoke({"path": "a.txt"})
    lines = out.content.splitlines()
    assert [ln.split("\t")[1] for ln in lines[:5]] == ["l1", "l2", "l3", "l4", "l5"]
    assert "l6" not in out.content
    assert "start_line" in lines[-1] and "6" in lines[-1] and "20" in lines[-1]
    assert out.ok and out.is_truncated is True


@pytest.mark.unit
async def test_an_explicit_range_wider_than_the_cap_is_still_cut(root: Path) -> None:
    put(root, "a.txt", "".join(f"l{i}\n" for i in range(1, 21)))
    out = await ViewFileTool(root, limits()).invoke(
        {"path": "a.txt", "start_line": 1, "end_line": 20}
    )
    assert out.is_truncated is True
    assert "l6" not in out.content


@pytest.mark.unit
async def test_a_range_that_ends_exactly_at_the_cap_has_no_continue_hint(root: Path) -> None:
    put(root, "a.txt", "".join(f"l{i}\n" for i in range(1, 6)))
    out = await ViewFileTool(root, limits()).invoke({"path": "a.txt"})
    assert out.is_truncated is False and "start_line" not in out.content


@pytest.mark.unit
async def test_a_start_beyond_the_end_fails_and_says_how_long_the_file_is(root: Path) -> None:
    put(root, "a.txt", "a\nb\nc\n")
    out = await ViewFileTool(root, limits()).invoke({"path": "a.txt", "start_line": 9})
    assert not out.ok and "3" in (out.error or "")


@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        {"path": "a.txt", "start_line": 0},
        {"path": "a.txt", "start_line": -2},
        {"path": "a.txt", "start_line": "x"},
        {"path": "a.txt", "start_line": True},
        {"path": "a.txt", "start_line": 4, "end_line": 2},
        {"path": "a.txt", "end_line": 1.5},
    ],
)
async def test_nonsense_ranges_are_a_failure_not_a_crash(
    root: Path, args: dict[str, object]
) -> None:
    put(root, "a.txt", "a\nb\nc\nd\n")
    out = await ViewFileTool(root, limits()).invoke(args)
    assert not out.ok and out.error


@pytest.mark.unit
async def test_missing_path_and_missing_file_and_directory_are_failures(root: Path) -> None:
    put(root, "d/x.txt", "x")
    tool = ViewFileTool(root, limits())
    assert not (await tool.invoke({})).ok
    assert not (await tool.invoke({"path": 3})).ok
    assert not (await tool.invoke({"path": "nope.txt"})).ok
    directory = await tool.invoke({"path": "d"})
    assert not directory.ok and "目录" in (directory.error or "")


@pytest.mark.unit
async def test_an_escape_comes_back_as_a_failure_observation(root: Path) -> None:
    out = await ViewFileTool(root, limits()).invoke({"path": "../../etc/passwd"})
    assert not out.ok and "工作区" in (out.error or "")


@pytest.mark.unit
async def test_a_binary_file_is_reported_not_dumped(root: Path) -> None:
    put(root, "img.bin", b"\x89PNG\r\n\x1a\n\x00\x00\x00" + b"\xff" * 50)
    out = await ViewFileTool(root, limits()).invoke({"path": "img.bin"})
    assert not out.ok
    assert "二进制" in (out.error or "") and "61" in (out.error or "")  # 字节数


@pytest.mark.unit
async def test_invalid_utf8_text_is_decoded_with_replacement(root: Path) -> None:
    put(root, "latin.txt", b"caf\xe9\nok\n")
    out = await ViewFileTool(root, limits()).invoke({"path": "latin.txt"})
    assert out.ok and "caf�" in out.content


@pytest.mark.unit
async def test_a_big_file_is_refused_without_a_range_but_readable_with_one(root: Path) -> None:
    put(root, "log.txt", "".join(f"line {i:04d}\n" for i in range(300)))  # 3000 字节 > 1000
    tool = ViewFileTool(root, limits())
    whole = await tool.invoke({"path": "log.txt"})
    assert not whole.ok and "start_line" in (whole.error or "")
    part = await tool.invoke({"path": "log.txt", "start_line": 200, "end_line": 202})
    assert part.ok
    assert [ln.split("\t")[1] for ln in part.content.splitlines()] == [
        "line 0199",
        "line 0200",
        "line 0201",
    ]


@pytest.mark.unit
async def test_a_file_without_a_trailing_newline_and_an_empty_file(root: Path) -> None:
    put(root, "a.txt", "a\nb")
    put(root, "empty.txt", "")
    tool = ViewFileTool(root, limits())
    assert (await tool.invoke({"path": "a.txt"})).content.splitlines()[-1] == "     2\tb"
    empty = await tool.invoke({"path": "empty.txt"})
    assert empty.ok and "空" in empty.content


@pytest.mark.unit
async def test_crlf_files_show_clean_lines(root: Path) -> None:
    put(root, "w.txt", b"a\r\nb\r\n")
    out = await ViewFileTool(root, limits()).invoke({"path": "w.txt"})
    assert out.content.splitlines() == ["     1\ta", "     2\tb"]


# ==============================================================================
# write_file
# ==============================================================================


@pytest.mark.unit
async def test_write_creates_the_file_and_its_parents(root: Path) -> None:
    tool = WriteFileTool(root, limits())
    assert tool.idempotent is False
    out = await tool.invoke({"path": "deep/er/a.txt", "content": "你好"})
    assert out.ok
    assert (root / "deep" / "er" / "a.txt").read_text(encoding="utf-8") == "你好"
    assert "deep/er/a.txt" in out.content and "2" in out.content


@pytest.mark.unit
async def test_write_overwrites_and_does_not_translate_newlines(root: Path) -> None:
    put(root, "a.txt", "old")
    out = await WriteFileTool(root, limits()).invoke({"path": "a.txt", "content": "x\r\ny\n"})
    assert out.ok
    assert (root / "a.txt").read_bytes() == b"x\r\ny\n"


@pytest.mark.unit
async def test_the_size_cap_counts_bytes_not_characters(root: Path) -> None:
    tool = WriteFileTool(root, limits(write_max_bytes=10))
    assert (await tool.invoke({"path": "ok.txt", "content": "a" * 10})).ok
    over = await tool.invoke({"path": "big.txt", "content": "你" * 4})  # 12 字节
    assert not over.ok and "10" in (over.error or "")
    assert not (root / "big.txt").exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        {"content": "x"},
        {"path": "a.txt"},
        {"path": "a.txt", "content": 3},
        {"path": "a.txt", "content": None},
        {"path": "../x.txt", "content": "x"},
        {"path": "/etc/x", "content": "x"},
    ],
)
async def test_bad_arguments_and_escapes_write_nothing(root: Path, args: dict[str, object]) -> None:
    out = await WriteFileTool(root, limits()).invoke(args)
    assert not out.ok
    assert list(root.iterdir()) == []  # noqa: ASYNC240


@pytest.mark.unit
async def test_writing_through_a_symlink_that_leaves_the_root_is_refused(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside / "target.txt", root / "sneaky.txt")
    out = await WriteFileTool(root, limits()).invoke({"path": "sneaky.txt", "content": "x"})
    assert not out.ok
    assert not (outside / "target.txt").exists()


@pytest.mark.unit
async def test_writing_over_a_directory_or_the_root_fails(root: Path) -> None:
    (root / "d").mkdir()
    tool = WriteFileTool(root, limits())
    assert not (await tool.invoke({"path": "d", "content": "x"})).ok
    assert not (await tool.invoke({"path": ".", "content": "x"})).ok


@pytest.mark.unit
async def test_the_permission_bits_of_an_existing_file_survive_an_overwrite(root: Path) -> None:
    script = put(root, "run.sh", "#!/bin/sh\n")
    script.chmod(0o755)
    await WriteFileTool(root, limits()).invoke({"path": "run.sh", "content": "#!/bin/sh\necho 2\n"})
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


@pytest.mark.unit
async def test_a_failed_write_leaves_the_original_intact_and_no_temp_files(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(root, "a.txt", "ORIGINAL")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    out = await WriteFileTool(root, limits()).invoke({"path": "a.txt", "content": "NEW"})
    assert not out.ok and "disk full" in (out.error or "")
    assert (root / "a.txt").read_text() == "ORIGINAL"
    assert [p.name for p in root.iterdir()] == ["a.txt"]  # noqa: ASYNC240


@pytest.mark.unit
async def test_the_overwritten_file_is_backed_up_when_a_backup_dir_is_given(
    root: Path, tmp_path: Path
) -> None:
    backup = tmp_path / "artifacts" / "t1" / "overwritten"
    put(root, "src/a.txt", "V1")
    tool = WriteFileTool(root, limits(), backup_dir=backup)
    out = await tool.invoke({"path": "src/a.txt", "content": "V2"})
    assert out.ok and "备份" in out.content
    backed = list(backup.rglob("*"))
    assert [p.read_text() for p in backed if p.is_file()] == ["V1"]

    await tool.invoke({"path": "src/a.txt", "content": "V3"})
    assert sorted(p.read_text() for p in backup.rglob("*") if p.is_file()) == ["V1", "V2"]


@pytest.mark.unit
async def test_a_new_file_needs_no_backup_and_a_missing_backup_dir_is_not_created(
    root: Path, tmp_path: Path
) -> None:
    backup = tmp_path / "bk"
    out = await WriteFileTool(root, limits(), backup_dir=backup).invoke(
        {"path": "new.txt", "content": "x"}
    )
    assert out.ok and not backup.exists()


@pytest.mark.unit
async def test_a_backup_that_cannot_be_made_stops_the_overwrite(root: Path, tmp_path: Path) -> None:
    """备份是写类工具“不得静默丢数据”的一半：备份失败宁可不写。"""
    blocker = tmp_path / "bk"
    blocker.write_text("我是文件")
    put(root, "a.txt", "PRECIOUS")
    out = await WriteFileTool(root, limits(), backup_dir=blocker / "sub").invoke(
        {"path": "a.txt", "content": "NEW"}
    )
    assert not out.ok and "备份" in (out.error or "")
    assert (root / "a.txt").read_text() == "PRECIOUS"
