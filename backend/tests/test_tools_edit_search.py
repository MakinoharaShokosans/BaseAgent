"""edit_file、list_dir、grep。出处：docs/04_tools/02_builtin_tools.md §5-6。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.builtin import EditFileTool, FileLimits, GrepTool, ListDirTool

LIMITS = FileLimits(view_max_lines=50, view_max_file_bytes=100_000, write_max_bytes=100_000)
IGNORED = [".git", "node_modules", "__pycache__"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def edit(root: Path, backup: Path | None = None) -> EditFileTool:
    return EditFileTool(root, LIMITS, backup_dir=backup)


# ==============================================================================
# edit_file
# ==============================================================================


@pytest.mark.unit
async def test_a_unique_match_is_replaced_and_the_rest_is_untouched(root: Path) -> None:
    (root / "a.py").write_text("def f():\n    return 1\n\ndef g():\n    return 2\n")
    result = await edit(root).invoke(
        {"path": "a.py", "old_string": "    return 1\n", "new_string": "    return 10\n"}
    )
    assert result.ok and "替换 1 处" in result.content
    assert (root / "a.py").read_text() == "def f():\n    return 10\n\ndef g():\n    return 2\n"


@pytest.mark.unit
async def test_ambiguous_match_fails_with_count_and_line_numbers(root: Path) -> None:
    (root / "a.txt").write_text("x = 1\ny = 2\nx = 1\n")
    result = await edit(root).invoke(
        {"path": "a.txt", "old_string": "x = 1", "new_string": "x = 3"}
    )
    assert result.failed and "2 次" in result.content and "[1, 3]" in result.content
    assert (root / "a.txt").read_text() == "x = 1\ny = 2\nx = 1\n"


@pytest.mark.unit
async def test_replace_all_replaces_every_occurrence(root: Path) -> None:
    (root / "a.txt").write_text("foo foo foo")
    result = await edit(root).invoke(
        {"path": "a.txt", "old_string": "foo", "new_string": "bar", "replace_all": True}
    )
    assert result.ok and "替换 3 处" in result.content
    assert (root / "a.txt").read_text() == "bar bar bar"


@pytest.mark.unit
async def test_no_match_reports_the_closest_line(root: Path) -> None:
    (root / "a.py").write_text("import os\n\ndef compute_total(items):\n    pass\n")
    result = await edit(root).invoke(
        {"path": "a.py", "old_string": "def compute_totl(items):", "new_string": "x"}
    )
    assert result.failed and "没有找到" in result.content
    assert "compute_total" in result.content and "第 3 行" in result.content


@pytest.mark.unit
async def test_crlf_files_keep_their_line_endings(root: Path) -> None:
    (root / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = await edit(root).invoke(
        {"path": "w.txt", "old_string": "one\ntwo\n", "new_string": "uno\ndos\n"}
    )
    assert result.ok
    assert (root / "w.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"


@pytest.mark.unit
async def test_permissions_are_preserved_and_a_backup_is_taken(root: Path, tmp_path: Path) -> None:
    script = root / "run.sh"
    script.write_text("echo old\n")
    script.chmod(0o755)
    backups = tmp_path / "bk"
    result = await edit(root, backups).invoke(
        {"path": "run.sh", "old_string": "old", "new_string": "new"}
    )
    assert result.ok and "已备份" in result.content
    assert script.stat().st_mode & 0o777 == 0o755
    assert (backups / "run.sh").read_text() == "echo old\n"
    assert not [p for p in root.iterdir() if p.name.startswith(".aegis-")]  # noqa: ASYNC240


@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"path": "a.txt", "old_string": "", "new_string": "x"},
        {"path": "a.txt", "old_string": "same", "new_string": "same"},
        {"path": "a.txt", "old_string": 1, "new_string": "x"},
        {"path": "missing.txt", "old_string": "a", "new_string": "b"},
        {"path": ".", "old_string": "a", "new_string": "b"},
    ],
)
async def test_bad_arguments_and_targets_are_failures_not_exceptions(
    root: Path, args: dict
) -> None:  # type: ignore[type-arg]
    (root / "a.txt").write_text("same")
    assert (await edit(root).invoke(args)).failed


@pytest.mark.security
async def test_edit_cannot_leave_the_workspace_or_follow_a_link_out(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    (root / "link.txt").symlink_to(outside)
    for path in ("../secret.txt", "/etc/hostname", "link.txt"):
        r = await edit(root).invoke({"path": path, "old_string": "top", "new_string": "x"})
        assert r.failed, path
    assert outside.read_text() == "top secret"


@pytest.mark.unit
async def test_binary_and_oversized_results_are_refused(root: Path) -> None:
    (root / "b.bin").write_bytes(b"\xff\xfe\x00\x01")
    assert (await edit(root).invoke({"path": "b.bin", "old_string": "a", "new_string": "b"})).failed
    (root / "a.txt").write_text("x")
    big = await edit(root).invoke({"path": "a.txt", "old_string": "x", "new_string": "y" * 200_000})
    assert big.failed and "超过上限" in big.content and (root / "a.txt").read_text() == "x"


# ==============================================================================
# list_dir
# ==============================================================================


def lister(root: Path, cap: int = 200) -> ListDirTool:
    return ListDirTool(root, ignored_dirs=IGNORED, max_entries=cap)


@pytest.fixture
def tree(root: Path) -> Path:
    for d in ("src/pkg", "node_modules/x", ".git", "tests", "__pycache__"):
        (root / d).mkdir(parents=True)
    for f in (
        "README.md",
        "src/main.py",
        "src/pkg/util.py",
        "node_modules/x/i.js",
        ".env",
        "tests/t.py",
    ):
        (root / f).write_text("x")
    return root


@pytest.mark.unit
async def test_list_dir_shows_directories_first_and_hides_noise(tree: Path) -> None:
    result = await lister(tree).invoke({})
    lines = result.content.splitlines()
    assert lines == ["src/", "  pkg/", "  main.py", "tests/", "  t.py", "README.md"]
    assert "node_modules" not in result.content and ".git" not in result.content
    assert ".env" not in result.content and "__pycache__" not in result.content


@pytest.mark.unit
async def test_depth_and_subpath(tree: Path) -> None:
    shallow = await lister(tree).invoke({"depth": 1})
    assert shallow.content.splitlines() == ["src/", "tests/", "README.md"]
    deep = await lister(tree).invoke({"path": "src", "depth": 3})
    assert "pkg/" in deep.content and "util.py" in deep.content
    assert (await lister(tree).invoke({"depth": 99})).ok  # 夹到上限而不是报错


@pytest.mark.unit
async def test_entry_cap_truncates_with_a_hint(tree: Path) -> None:
    result = await lister(tree, cap=3).invoke({"depth": 3})
    assert result.is_truncated and "还有更多" in result.content
    assert len([ln for ln in result.content.splitlines() if not ln.startswith("[")]) == 3


@pytest.mark.unit
async def test_list_dir_failures_and_empty(tree: Path) -> None:
    (tree / "empty").mkdir()
    assert (await lister(tree).invoke({"path": "empty"})).content == "（空目录）"
    assert (await lister(tree).invoke({"path": "nope"})).failed
    assert (await lister(tree).invoke({"path": "README.md"})).failed
    assert (await lister(tree).invoke({"path": "../"})).failed


@pytest.mark.security
async def test_list_dir_does_not_show_links_pointing_outside(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (root / "ok.txt").write_text("x")
    (root / "escape").symlink_to(outside)
    listing = (await lister(root).invoke({"depth": 3})).content
    assert "ok.txt" in listing and "escape" not in listing and "secret" not in listing


# ==============================================================================
# grep
# ==============================================================================


def grepper(root: Path, cap: int = 50) -> GrepTool:
    return GrepTool(root, ignored_dirs=IGNORED, max_results=cap, max_file_bytes=10_000)


@pytest.fixture
def code(root: Path) -> Path:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text(
        "import os\n\ndef alpha():\n    return 1\n\ndef beta():\n    return 2\n"
    )
    (root / "src" / "b.js").write_text("function alpha() {}\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("function alpha() {}\n")
    (root / "bin.dat").write_bytes(b"alpha\x00\x01")
    (root / "big.txt").write_text("alpha " * 5000)
    return root


@pytest.mark.unit
async def test_grep_finds_lines_with_paths_and_numbers_and_skips_noise(code: Path) -> None:
    result = await grepper(code).invoke({"pattern": r"def \w+"})
    assert result.content.splitlines() == ["src/a.py:3:def alpha():", "src/a.py:6:def beta():"]
    everywhere = await grepper(code).invoke({"pattern": "alpha"})
    assert "node_modules" not in everywhere.content and "bin.dat" not in everywhere.content
    assert "big.txt" not in everywhere.content  # 超过大小上限的文件被跳过
    assert "src/b.js:1:function alpha() {}" in everywhere.content


@pytest.mark.unit
async def test_glob_path_case_and_fixed_options(code: Path) -> None:
    only_js = await grepper(code).invoke({"pattern": "alpha", "glob": "*.js"})
    assert only_js.content.splitlines() == ["src/b.js:1:function alpha() {}"]
    one_file = await grepper(code).invoke({"pattern": "return", "path": "src/a.py"})
    assert len(one_file.content.splitlines()) == 2
    assert "没有匹配" in (await grepper(code).invoke({"pattern": "ALPHA", "glob": "*.py"})).content
    folded = await grepper(code).invoke({"pattern": "ALPHA", "glob": "*.py", "ignore_case": True})
    assert "src/a.py:3" in folded.content
    literal = await grepper(code).invoke({"pattern": "alpha()", "fixed": True, "glob": "*.js"})
    assert "src/b.js:1" in literal.content
    bad_as_regex = await grepper(code).invoke({"pattern": "alpha("})
    assert bad_as_regex.failed and "正则" in bad_as_regex.content


@pytest.mark.unit
async def test_context_lines_use_dash_separators(code: Path) -> None:
    result = await grepper(code).invoke({"pattern": "def beta", "context": 1, "path": "src/a.py"})
    assert result.content.splitlines() == [
        "src/a.py-5-",
        "src/a.py:6:def beta():",
        "src/a.py-7-    return 2",
    ]


@pytest.mark.unit
async def test_hit_cap_truncates(root: Path) -> None:
    (root / "many.txt").write_text("hit\n" * 100)
    result = await grepper(root, cap=5).invoke({"pattern": "hit"})
    assert (
        result.is_truncated
        and len([ln for ln in result.content.splitlines() if ln.startswith("many")]) == 5
    )
    assert "5 条命中上限" in result.content


@pytest.mark.unit
async def test_grep_argument_failures(code: Path) -> None:
    for args in ({}, {"pattern": ""}, {"pattern": "x" * 600}, {"pattern": "a", "path": "nope"}):
        assert (await grepper(code).invoke(args)).failed
    assert (await grepper(code).invoke({"pattern": "a", "path": "../"})).failed


@pytest.mark.security
async def test_grep_cannot_read_outside_through_links_or_paths(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "secret.txt").write_text("password=hunter2\n")
    (root / "dirlink").symlink_to(outside)
    (root / "filelink.txt").symlink_to(outside / "secret.txt")
    result = await grepper(root).invoke({"pattern": "hunter2"})
    assert "hunter2" not in result.content
    for path in ("../out", str(outside), "dirlink"):
        assert (
            await grepper(root).invoke({"pattern": "hunter2", "path": path})
        ).failed or "hunter2" not in (
            await grepper(root).invoke({"pattern": "hunter2", "path": path})
        ).content
    _ = os
