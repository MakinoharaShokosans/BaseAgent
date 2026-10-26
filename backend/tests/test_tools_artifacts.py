"""read_artifact：只读本任务登记过的产物。出处：02_builtin_tools.md §4，观察值治理 §6。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.builtin import FileLimits, ReadArtifactTool, TaskArtifacts, producer_candidates


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    (root / "t1").mkdir(parents=True)
    return root.resolve()


def make(
    store: Path, entries: dict[str, str], *, trusted: tuple[str, ...] = ("bash",), **kw: int
) -> ReadArtifactTool:
    held = TaskArtifacts()
    held.bind(entries)
    return ReadArtifactTool(
        held,
        store,
        FileLimits(
            view_max_lines=kw.get("lines", 4), view_max_file_bytes=10**6, write_max_bytes=10
        ),
        grep_max_results=kw.get("grep", 3),
        trusted_producers=trusted,
    )


def log(store: Path, name: str, text: str | bytes) -> str:
    path = store / "t1" / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return str(path)


def lines_of(content: str) -> list[str]:
    return [ln.split("\t", 1)[1] for ln in content.splitlines() if "\t" in ln]


@pytest.mark.unit
async def test_a_registered_artifact_is_read_with_line_numbers(store: Path) -> None:
    tool = make(store, {"obs_step_3_bash": log(store, "obs_step_3_bash.log", "a\nb\nc\n")})
    assert tool.idempotent is True
    out = await tool.invoke({"artifact_id": "obs_step_3_bash"})
    assert out.ok and lines_of(out.content) == ["a", "b", "c"]


@pytest.mark.unit
async def test_the_handle_form_shown_in_summaries_is_accepted(store: Path) -> None:
    tool = make(store, {"obs_step_3_bash": log(store, "obs_step_3_bash.log", "a\n")})
    out = await tool.invoke({"artifact_id": "artifact://obs_step_3_bash"})
    assert out.ok and lines_of(out.content) == ["a"]


@pytest.mark.unit
async def test_paging_matches_view_file(store: Path) -> None:
    text = "".join(f"l{i}\n" for i in range(1, 11))
    tool = make(store, {"a": log(store, "a.log", text)})
    first = await tool.invoke({"artifact_id": "a"})
    assert lines_of(first.content)[:4] == ["l1", "l2", "l3", "l4"]
    assert first.is_truncated and "start_line" in first.content and "5" in first.content
    second = await tool.invoke({"artifact_id": "a", "start_line": 5, "end_line": 6})
    assert lines_of(second.content) == ["l5", "l6"]
    beyond = await tool.invoke({"artifact_id": "a", "start_line": 99})
    assert not beyond.ok and "10" in (beyond.error or "")


@pytest.mark.unit
@pytest.mark.parametrize("bad", [None, "", 5, "nope", "../t1/a.log", "/etc/passwd", "t1/a.log"])
async def test_only_registered_ids_work_never_paths(store: Path, bad: object) -> None:
    tool = make(store, {"a": log(store, "a.log", "x\n")})
    out = await tool.invoke({"artifact_id": bad})
    assert not out.ok


@pytest.mark.unit
async def test_the_failure_lists_the_known_ids_but_not_their_paths(store: Path) -> None:
    path = log(store, "a.log", "x\n")
    out = await make(store, {"obs_step_1_bash": path}).invoke({"artifact_id": "zzz"})
    assert "obs_step_1_bash" in (out.error or "")
    assert path not in (out.error or "")


@pytest.mark.unit
async def test_rebinding_replaces_the_set_so_another_tasks_artifacts_vanish(store: Path) -> None:
    held = TaskArtifacts()
    held.bind({"mine": log(store, "mine.log", "1\n")})
    tool = ReadArtifactTool(
        held, store, FileLimits(4, 10**6, 10), grep_max_results=3, trusted_producers=()
    )
    assert (await tool.invoke({"artifact_id": "mine"})).ok
    held.bind({"other": log(store, "other.log", "2\n")})
    assert not (await tool.invoke({"artifact_id": "mine"})).ok


@pytest.mark.unit
async def test_a_registry_entry_pointing_outside_the_artifact_root_is_refused(
    store: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    link = store / "t1" / "l.log"
    os.symlink(secret, link)
    tool = make(store, {"direct": str(secret), "via_link": str(link)})
    for ident in ("direct", "via_link"):
        out = await tool.invoke({"artifact_id": ident})
        assert not out.ok and "TOP SECRET" not in out.content


@pytest.mark.unit
async def test_a_registered_but_deleted_file_is_a_failure(store: Path) -> None:
    out = await make(store, {"gone": str(store / "t1" / "gone.log")}).invoke(
        {"artifact_id": "gone"}
    )
    assert not out.ok and "gone" in (out.error or "")


@pytest.mark.unit
async def test_grep_returns_matching_lines_with_their_numbers_up_to_the_cap(store: Path) -> None:
    text = "ok\nERROR one\nok\nERROR two\nERROR three\nERROR four\n"
    tool = make(store, {"a": log(store, "a.log", text)})
    out = await tool.invoke({"artifact_id": "a", "grep": "ERROR"})
    assert out.ok
    body = out.content.splitlines()
    assert body[:3] == ["     2\tERROR one", "     4\tERROR two", "     5\tERROR three"]
    assert out.is_truncated and "ERROR four" not in out.content
    assert "3" in body[-1]  # 提示上限


@pytest.mark.unit
async def test_grep_with_no_match_and_with_a_bad_pattern(store: Path) -> None:
    tool = make(store, {"a": log(store, "a.log", "x\n")})
    none = await tool.invoke({"artifact_id": "a", "grep": "zzz"})
    assert none.ok and "没有" in none.content
    bad = await tool.invoke({"artifact_id": "a", "grep": "("})
    assert not bad.ok and "正则" in (bad.error or "")
    assert not (await tool.invoke({"artifact_id": "a", "grep": 3})).ok


@pytest.mark.unit
async def test_trust_follows_the_producing_tool(store: Path) -> None:
    entries = {
        "obs_step_2_bash": log(store, "1.log", "a\n"),
        "obs_step_2_bash_2": log(store, "2.log", "a\n"),
        "step_4_bash": log(store, "3.log", "a\n"),
        "obs_step_5_mcp_web": log(store, "4.log", "a\n"),
        "weird": log(store, "5.log", "a\n"),
    }
    tool = make(store, entries, trusted=("bash",))
    got = {i: (await tool.invoke({"artifact_id": i})).trust for i in entries}
    assert got == {
        "obs_step_2_bash": "trusted",
        # 同一步的重名后缀与带数字的工具名分不开：两种读法都可信才算可信，否则多一层信封
        "obs_step_2_bash_2": "untrusted",
        "step_4_bash": "trusted",
        "obs_step_5_mcp_web": "untrusted",
        "weird": "untrusted",  # 认不出来源：取最保守值
    }


@pytest.mark.unit
def test_an_ambiguous_tool_name_must_be_trusted_under_every_reading() -> None:
    assert producer_candidates("obs_step_1_x_2") == ("x_2", "x")
    assert producer_candidates("obs_step_1_bash") == ("bash",)
    assert producer_candidates("step_7_bash") == ("bash",)
    assert producer_candidates("nonsense") == ()


@pytest.mark.unit
async def test_non_utf8_content_is_replaced_not_fatal(store: Path) -> None:
    tool = make(store, {"a": log(store, "a.log", b"caf\xe9\n")})
    out = await tool.invoke({"artifact_id": "a"})
    assert out.ok and "caf�" in out.content
