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
async def test_a_successful_edit_shows_the_diff_to_verify(root: Path) -> None:
    (root / "a.py").write_text("a\nb\nc\nd\ne\nf\n")
    result = await edit(root).invoke({"path": "a.py", "old_string": "c\n", "new_string": "C\nC2\n"})
    assert result.ok
    lines = result.content.splitlines()
    assert lines[1].startswith("@@") and "-c" in lines and "+C" in lines and "+C2" in lines
    assert " b" in lines and " d" in lines  # 前后各带上下文


@pytest.mark.unit
async def test_multi_line_old_string_matches_across_lines(root: Path) -> None:
    (root / "a.py").write_text("def f(\n    a,\n    b,\n):\n    pass\n")
    result = await edit(root).invoke(
        {"path": "a.py", "old_string": "    a,\n    b,\n", "new_string": "    a, b,\n"}
    )
    assert result.ok and (root / "a.py").read_text() == "def f(\n    a, b,\n):\n    pass\n"


@pytest.mark.unit
async def test_regex_edit_uses_capture_groups_and_line_anchors(root: Path) -> None:
    (root / "a.py").write_text("x = 1\ny = 2\n")
    result = await edit(root).invoke(
        {
            "path": "a.py",
            "old_string": r"^(\w) = (\d)$",
            "new_string": r"\1: \2",
            "regex": True,
            "replace_all": True,
        }
    )
    assert result.ok and "替换 2 处" in result.content
    assert (root / "a.py").read_text() == "x: 1\ny: 2\n"


@pytest.mark.unit
async def test_regex_edit_is_unique_by_default_and_dotall_spans_lines(root: Path) -> None:
    (root / "a.txt").write_text("<a>1</a>\n<a>2</a>\n")
    many = await edit(root).invoke(
        {"path": "a.txt", "old_string": r"<a>\d</a>", "new_string": "X", "regex": True}
    )
    assert many.failed and "匹配了 2 处" in many.content
    span = await edit(root).invoke(
        {
            "path": "a.txt",
            "old_string": r"<a>1.*?</a>\n<a>2",
            "new_string": "Z",
            "regex": True,
            "dotall": True,
        }
    )
    assert span.ok and (root / "a.txt").read_text() == "Z</a>\n"
    bad = await edit(root).invoke(
        {"path": "a.txt", "old_string": "(", "new_string": "x", "regex": True}
    )
    assert bad.failed and "正则无法编译" in bad.content
    ref = await edit(root).invoke(
        {"path": "a.txt", "old_string": "Z", "new_string": r"\9", "regex": True}
    )
    assert ref.failed and "捕获组" in ref.content


@pytest.mark.unit
async def test_a_line_range_disambiguates_repeated_text(root: Path) -> None:
    (root / "a.txt").write_text("x = 1\ny = 2\nx = 1\n")
    result = await edit(root).invoke(
        {"path": "a.txt", "old_string": "x = 1", "new_string": "x = 3", "start_line": 3}
    )
    assert result.ok and (root / "a.txt").read_text() == "x = 1\ny = 2\nx = 3\n"
    out_of_range = await edit(root).invoke(
        {"path": "a.txt", "old_string": "y = 2", "new_string": "y = 9", "start_line": 3}
    )
    assert out_of_range.failed and "限定在第 3" in out_of_range.content


@pytest.mark.unit
async def test_a_batch_of_edits_is_applied_in_order_and_atomically(root: Path) -> None:
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    ok = await edit(root).invoke(
        {
            "path": "a.txt",
            "edits": [
                {"old_string": "one", "new_string": "1"},
                {"old_string": "two", "new_string": "2"},
            ],
        }
    )
    assert ok.ok and "替换 2 处" in ok.content and (root / "a.txt").read_text() == "1\n2\nthree\n"
    failing = await edit(root).invoke(
        {
            "path": "a.txt",
            "edits": [
                {"old_string": "three", "new_string": "3"},
                {"old_string": "missing", "new_string": "x"},
            ],
        }
    )
    assert failing.failed and "edits[1]" in failing.content
    assert (root / "a.txt").read_text() == "1\n2\nthree\n"  # 第一处也没有写


@pytest.mark.unit
async def test_batch_arguments_are_validated(root: Path) -> None:
    (root / "a.txt").write_text("a")
    tool = edit(root)
    assert (await tool.invoke({"path": "a.txt", "edits": []})).failed
    assert (await tool.invoke({"path": "a.txt", "edits": ["x"]})).failed
    both = await tool.invoke(
        {
            "path": "a.txt",
            "old_string": "a",
            "new_string": "b",
            "edits": [{"old_string": "a", "new_string": "c"}],
        }
    )
    assert both.failed and "只能给一个" in both.content


@pytest.mark.unit
async def test_crlf_files_accept_either_newline_style_and_keep_their_endings(root: Path) -> None:
    (root / "w.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = await edit(root).invoke(
        {"path": "w.txt", "old_string": "one\r\ntwo\r\n", "new_string": "uno\r\ndos\r\n"}
    )
    assert result.ok and "CRLF" in result.content
    assert (root / "w.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"
    regex = await edit(root).invoke(
        {"path": "w.txt", "old_string": r"dos\nthree", "new_string": "tres", "regex": True}
    )
    assert regex.ok and (root / "w.txt").read_bytes() == b"uno\r\ntres\r\n"


@pytest.mark.unit
async def test_a_bom_survives_an_edit_and_mixed_endings_are_named_on_failure(root: Path) -> None:
    (root / "b.txt").write_bytes(b"\xef\xbb\xbfhello\nworld\n")
    result = await edit(root).invoke({"path": "b.txt", "old_string": "hello", "new_string": "hi"})
    assert result.ok and (root / "b.txt").read_bytes() == b"\xef\xbb\xbfhi\nworld\n"
    (root / "m.txt").write_bytes(b"a\r\nb\nc\n")
    miss = await edit(root).invoke({"path": "m.txt", "old_string": "a\nb", "new_string": "x"})
    assert miss.failed and "换行混合" in miss.content


@pytest.mark.unit
async def test_a_no_op_edit_is_refused_and_non_utf8_files_are_not_edited(root: Path) -> None:
    (root / "g.txt").write_bytes("你好\n".encode("gb18030"))
    gbk = await edit(root).invoke({"path": "g.txt", "old_string": "你好", "new_string": "x"})
    assert gbk.failed and "不是 UTF-8" in gbk.content
    (root / "a.txt").write_text("abc")
    same = await edit(root).invoke(
        {"path": "a.txt", "old_string": "a", "new_string": "a", "regex": True}
    )
    assert same.failed and "完全相同" in same.content


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
    assert not [p for p in root.iterdir() if p.name.startswith(".baseagent-")]  # noqa: ASYNC240


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
    assert "已显示第 1-5 处" in result.content and "offset=5" in result.content


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


# ==============================================================================
# grep 的新能力：输出模式、跨行、路径通配、前后文分开、分页、行号一致
# ==============================================================================


@pytest.mark.unit
async def test_output_modes_files_and_count(code: Path) -> None:
    files = await grepper(code).invoke({"pattern": "alpha", "output_mode": "files"})
    assert files.content.splitlines() == ["src/a.py", "src/b.js"]
    count = await grepper(code).invoke({"pattern": "def ", "output_mode": "count"})
    assert count.content.splitlines() == ["src/a.py:2"]
    bad = await grepper(code).invoke({"pattern": "x", "output_mode": "tree"})
    assert bad.failed


@pytest.mark.unit
async def test_multiline_patterns_span_lines_and_report_every_line(code: Path) -> None:
    single = await grepper(code).invoke({"pattern": r"alpha\(\):\n    return"})
    assert single.content.startswith("没有匹配")  # 默认逐行匹配，\\n 永远对不上
    result = await grepper(code).invoke(
        {"pattern": r"def alpha\(\):\n    return \d", "multiline": True}
    )
    assert result.content.splitlines() == ["src/a.py:3:def alpha():", "src/a.py:4:    return 1"]


@pytest.mark.unit
async def test_path_globs_and_excludes(code: Path) -> None:
    (code / "tests").mkdir()
    (code / "tests" / "t.py").write_text("def alpha(): pass\n")
    only_src = await grepper(code).invoke(
        {"pattern": "alpha", "glob": "src/**/*.py", "output_mode": "files"}
    )
    assert only_src.content.splitlines() == ["src/a.py"]
    braces = await grepper(code).invoke(
        {"pattern": "alpha", "glob": "**/*.{py,js}", "output_mode": "files"}
    )
    assert braces.content.splitlines() == ["src/a.py", "src/b.js", "tests/t.py"]
    excluded = await grepper(code).invoke(
        {"pattern": "alpha", "glob": "*.py", "exclude_glob": "tests/**", "output_mode": "files"}
    )
    assert excluded.content.splitlines() == ["src/a.py"]


@pytest.mark.unit
async def test_before_and_after_context_are_independent(code: Path) -> None:
    result = await grepper(code).invoke(
        {"pattern": "def beta", "path": "src/a.py", "before": 1, "after": 0}
    )
    assert result.content.splitlines() == ["src/a.py-5-", "src/a.py:6:def beta():"]
    after = await grepper(code).invoke({"pattern": "def beta", "path": "src/a.py", "after": 1})
    assert after.content.splitlines() == ["src/a.py:6:def beta():", "src/a.py-7-    return 2"]


@pytest.mark.unit
async def test_offset_pages_through_the_results(root: Path) -> None:
    (root / "many.txt").write_text("".join(f"hit {i}\n" for i in range(1, 11)))
    tool = grepper(root, cap=4)
    first = await tool.invoke({"pattern": "hit"})
    assert "many.txt:4:hit 4" in first.content and "offset=4" in first.content
    second = await tool.invoke({"pattern": "hit", "offset": 4})
    assert second.content.splitlines()[0] == "many.txt:5:hit 5" and "offset=8" in second.content
    last = await tool.invoke({"pattern": "hit", "offset": 8})
    assert last.content.splitlines() == ["many.txt:9:hit 9", "many.txt:10:hit 10"]  # 末页没有页脚
    beyond = await tool.invoke({"pattern": "hit", "offset": 50})
    assert beyond.ok and "已超出" in beyond.content
    assert (await tool.invoke({"pattern": "hit", "offset": -1})).failed


@pytest.mark.unit
async def test_grep_line_numbers_match_view_file_even_with_odd_separators(root: Path) -> None:
    from tools.builtin import ViewFileTool

    (root / "m.txt").write_text("a\x0cb\nneedle\u2028x\nz\n")  # 换页符与 U+2028 不是换行
    found = await grepper(root).invoke({"pattern": "needle", "output_mode": "content"})
    assert found.content.startswith("m.txt:2:")
    viewed = await ViewFileTool(root, LIMITS).invoke(
        {"path": "m.txt", "start_line": 2, "end_line": 2}
    )
    assert "needle" in viewed.content


@pytest.mark.unit
async def test_non_utf8_files_are_searched_and_long_lines_are_clipped(root: Path) -> None:
    (root / "g.txt").write_bytes("你好，世界\n".encode("gb18030"))
    (root / "long.txt").write_text("needle " + "x" * 3000 + "\n")
    gbk = await grepper(root).invoke({"pattern": "世界", "path": "g.txt"})
    assert gbk.content == "g.txt:1:你好，世界"
    long = await grepper(root).invoke({"pattern": "needle", "path": "long.txt"})
    assert "已截断" in long.content


# ==============================================================================
# list_dir 的新能力：按通配符找文件、显示大小
# ==============================================================================


@pytest.mark.unit
async def test_list_dir_pattern_finds_files_across_directories(tree: Path) -> None:
    (tree / "src").mkdir(exist_ok=True)
    (tree / "src" / "deep").mkdir(exist_ok=True)
    (tree / "src" / "deep" / "x.test.ts").write_text("1")
    (tree / "src" / "y.test.ts").write_text("2")
    (tree / "src" / "y.ts").write_text("3")
    found = await lister(tree).invoke({"pattern": "**/*.test.ts"})
    assert found.content.splitlines() == ["src/deep/x.test.ts", "src/y.test.ts"]
    scoped = await lister(tree).invoke({"pattern": "*.test.ts", "path": "src/deep"})
    assert scoped.content.splitlines() == ["x.test.ts"]
    none = await lister(tree).invoke({"pattern": "*.rs"})
    assert none.ok and "没有文件匹配" in none.content


@pytest.mark.unit
async def test_list_dir_pattern_is_capped_and_details_show_sizes(tree: Path) -> None:
    (tree / "docs").mkdir(exist_ok=True)
    for i in range(6):
        (tree / "docs" / f"n{i}.md").write_text("x" * 2048)
    capped = await lister(tree, cap=3).invoke({"pattern": "docs/*.md", "details": True})
    lines = capped.content.splitlines()
    assert lines[0] == "docs/n0.md  (2.0 KB)" and "共 6 个文件匹配" in lines[-1]
    assert capped.is_truncated is True
    listed = await lister(tree).invoke({"path": "docs", "details": True})
    assert "n0.md  (2.0 KB)" in listed.content


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.py", "a.py", True),
        ("*.py", "src/a.py", True),  # 不含 / 只看文件名
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/deep/a.py", False),  # * 不跨目录
        ("src/**/*.py", "src/deep/a.py", True),
        ("src/**/*.py", "src/a.py", True),
        ("**/*.{ts,tsx}", "a/b/c.tsx", True),
        ("a?.txt", "ab.txt", True),
        ("a?.txt", "abc.txt", False),
        ("docs/**", "docs/x/y.md", True),
    ],
)
def test_glob_translation(pattern: str, path: str, expected: bool) -> None:
    from tools.builtin.textio import glob_to_regex

    assert bool(glob_to_regex(pattern).fullmatch(path)) is expected
