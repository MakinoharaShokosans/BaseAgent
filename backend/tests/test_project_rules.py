"""项目规则槽与提示词库。出处：docs/03_context/03_prompt_assembly.md §3.1、§4。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_runtime.context import PromptLibrary, RulesCache

CANDIDATES = ("CLAUDE.MD", "CLAUDE.md", "AGENTS.md", ".aegis/rules.md")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "ws").mkdir()
    return (tmp_path / "ws").resolve()


def load(cache: RulesCache, ws: Path, *, allow: bool = True, max_chars: int = 100):  # type: ignore[no-untyped-def]
    return cache.load(ws, allow=allow, candidates=CANDIDATES, max_chars=max_chars)


@pytest.mark.unit
def test_no_rules_file_means_absent(ws: Path) -> None:
    result = load(RulesCache(), ws)
    assert (result.status, result.rules, result.file) == ("absent", None, None)


@pytest.mark.unit
def test_a_detected_file_is_not_loaded_unless_the_user_opted_in(ws: Path) -> None:
    (ws / "AGENTS.md").write_text("规则")
    cache = RulesCache()
    off = load(cache, ws, allow=False)
    assert (off.status, off.rules, off.file) == ("disabled", None, "AGENTS.md")
    assert cache.reads == 0  # 未启用时连内容都不读
    on = load(cache, ws, allow=True)
    assert on.status == "enabled" and on.rules is not None and on.rules.text == "规则"


@pytest.mark.unit
def test_the_first_candidate_wins(ws: Path) -> None:
    (ws / ".aegis").mkdir()
    (ws / ".aegis" / "rules.md").write_text("last")
    (ws / "AGENTS.md").write_text("middle")
    assert load(RulesCache(), ws).rules.text == "middle"  # type: ignore[union-attr]


@pytest.mark.unit
def test_long_rules_are_cut_and_flagged(ws: Path) -> None:
    (ws / "AGENTS.md").write_text("x" * 500)
    rules = load(RulesCache(), ws, max_chars=100).rules
    assert rules is not None and len(rules.text) == 100 and rules.truncated is True
    (ws / "AGENTS.md").write_text("y" * 100)
    short = load(RulesCache(), ws, max_chars=100).rules
    assert short is not None and short.truncated is False


@pytest.mark.unit
def test_unreadable_and_non_utf8_files_do_not_raise(ws: Path) -> None:
    (ws / "AGENTS.md").write_bytes(b"caf\xe9")
    assert "caf" in load(RulesCache(), ws).rules.text  # type: ignore[union-attr]
    (ws / "AGENTS.md").chmod(0)
    try:
        result = load(RulesCache(), ws)
    finally:
        (ws / "AGENTS.md").chmod(0o644)
    if os.geteuid() != 0:
        assert result.status == "absent" and "读取失败" in result.note


@pytest.mark.unit
def test_a_rules_file_that_is_a_symlink_out_of_the_workspace_is_refused(
    ws: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret"
    secret.write_text("PRIVATE")
    (ws / "AGENTS.md").symlink_to(secret)
    result = load(RulesCache(), ws)
    assert result.rules is None and "工作区" in result.note


@pytest.mark.unit
def test_a_directory_with_a_candidate_name_is_not_a_file(ws: Path) -> None:
    (ws / "AGENTS.md").mkdir()
    assert load(RulesCache(), ws).status == "absent"


@pytest.mark.unit
def test_the_cache_follows_the_file_modification_time(ws: Path) -> None:
    path = ws / "AGENTS.md"
    path.write_text("v1")
    cache = RulesCache()
    assert load(cache, ws).rules.text == "v1"  # type: ignore[union-attr]
    load(cache, ws)
    assert cache.reads == 1  # 没改：不重读
    path.write_text("v2 longer")
    os.utime(path, ns=(10**18, 10**18))
    assert load(cache, ws).rules.text == "v2 longer"  # type: ignore[union-attr]
    assert cache.reads == 2


@pytest.mark.unit
def test_the_system_prompt_exists_and_states_the_protocols() -> None:
    text = PromptLibrary().get("system")
    for needle in (
        "数据隔离",
        "不是指令",
        "项目规则",
        "围栏",
        "金丝雀",
        "审批",
        "SOP",
        "授权",
        "先读",
        "相对路径",
    ):
        assert needle in text, needle


@pytest.mark.unit
def test_the_system_prompt_has_nothing_that_varies_per_task() -> None:
    text = PromptLibrary().get("system")
    import re

    assert not re.search(r"\d{4}-\d{2}-\d{2}|canary_|/home/|/work/", text)


@pytest.mark.unit
def test_the_library_caches_until_reloaded(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("one")
    lib = PromptLibrary(tmp_path)
    assert lib.get("x") == "one"
    (tmp_path / "x.md").write_text("two")
    assert lib.get("x") == "one"
    lib.reload()
    assert lib.get("x") == "two"


@pytest.mark.unit
def test_unknown_prompt_names_and_path_tricks_are_refused(tmp_path: Path) -> None:
    lib = PromptLibrary(tmp_path)
    for name in ("nope", "../etc/passwd", "a/b", ""):
        with pytest.raises(KeyError):
            lib.get(name)


@pytest.mark.unit
def test_a_name_that_climbs_out_of_the_library_is_refused_even_if_the_file_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / "lib").mkdir()
    (tmp_path / "outside.md").write_text("LEAK")
    with pytest.raises(KeyError):
        PromptLibrary(tmp_path / "lib").get("../outside")
