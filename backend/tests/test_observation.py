"""观察值治理：裁剪与落盘。出处：docs/03_context/01_observation_governance.md。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from agent_runtime.context import ObservationGovernor, ObservationLimits, ObservationStore


def count(text: str) -> int:
    """测试用的计数：一个字符一个 token，边界一目了然。"""
    return len(text)


def limits(**kw: int | tuple[str, ...]) -> ObservationLimits:
    base: dict[str, int | tuple[str, ...]] = {
        "max_tokens": 200,
        "head_lines": 3,
        "tail_lines": 4,
        "keyword_lines_limit": 5,
        "json_outline_max_depth": 3,
        "json_outline_array_items": 2,
        "scalar_preview_chars": 10,
        "exempt_tools": ("load_skill",),
        "exempt_multiplier": 4,
    }
    return ObservationLimits(**{**base, **kw})  # type: ignore[arg-type]


def governor(tmp_path: Path, **kw: int | tuple[str, ...]) -> ObservationGovernor:
    return ObservationGovernor(limits(**kw), count, ObservationStore(tmp_path / "artifacts"))


def numbered(n: int, width: int = 12) -> str:
    return "\n".join(f"line {i:04d}".ljust(width) for i in range(n))


# ==============================================================================
# 阈值
# ==============================================================================


@pytest.mark.unit
async def test_text_at_or_under_the_limit_is_untouched_with_no_side_effect(
    tmp_path: Path,
) -> None:
    g = governor(tmp_path)
    for text in ("", "x", "y" * 200):
        out = await g.govern(text, task_id="t1", step=1, tool_name="bash", taken=frozenset())
        assert out.summary == text
        assert out.artifact_id is None and out.truncated is False
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.unit
async def test_one_over_the_limit_is_shortened_and_saved(tmp_path: Path) -> None:
    g = governor(tmp_path)
    text = numbered(40)
    out = await g.govern(text, task_id="t1", step=3, tool_name="bash", taken=frozenset())

    assert out.truncated is True
    assert out.artifact_id == "obs_step_3_bash"
    assert out.artifact_path is not None
    assert Path(out.artifact_path).read_text(encoding="utf-8") == text  # noqa: ASYNC240
    assert "artifact://obs_step_3_bash" in out.summary
    assert len(out.summary) < len(text)


# ==============================================================================
# 文本
# ==============================================================================


@pytest.mark.unit
async def test_text_keeps_the_head_and_tail_lines_and_says_what_was_skipped(
    tmp_path: Path,
) -> None:
    out = await governor(tmp_path).govern(
        numbered(60), task_id="t", step=1, tool_name="bash", taken=frozenset()
    )
    lines = out.summary.splitlines()
    assert lines[:3] == [f"line {i:04d}".ljust(12) for i in range(3)]
    assert f"line {59:04d}".ljust(12) in lines[-3:] or any("0059" in ln for ln in lines)
    assert "line 0030" not in out.summary
    assert "省略" in out.summary


@pytest.mark.unit
async def test_error_keywords_in_the_middle_survive_up_to_the_limit(tmp_path: Path) -> None:
    middle = [f"noise {i:03d}" for i in range(60)]
    for i in range(20, 30):
        middle[i] = f"ERROR something broke {i}"
    text = "\n".join(["start", *middle, "end"])
    out = await governor(tmp_path, max_tokens=100).govern(
        text, task_id="t", step=1, tool_name="bash", taken=frozenset()
    )
    kept = [ln for ln in out.summary.splitlines() if "ERROR something broke" in ln]
    assert 1 <= len(kept) <= 5


@pytest.mark.unit
async def test_a_few_very_long_lines_are_still_bounded(tmp_path: Path) -> None:
    """行数不多但每行巨长（压缩过的 JSON 之外的东西，如 base64）：不能因为行数少就原样放行。"""
    text = "\n".join(["a" * 5000, "b" * 5000])
    out = await governor(tmp_path).govern(
        text, task_id="t", step=1, tool_name="bash", taken=frozenset()
    )
    assert out.truncated is True
    assert len(out.summary) < 1000


@pytest.mark.unit
async def test_the_keyword_search_ignores_case_and_covers_the_known_words(
    tmp_path: Path,
) -> None:
    text = "\n".join(["head"] * 3 + ["Traceback (most recent call last)"] + ["pad"] * 80)
    out = await governor(tmp_path, max_tokens=50).govern(
        text, task_id="t", step=1, tool_name="bash", taken=frozenset()
    )
    assert "Traceback" in out.summary


# ==============================================================================
# JSON
# ==============================================================================


@pytest.mark.unit
async def test_json_gets_a_valid_json_outline_never_a_character_cut(tmp_path: Path) -> None:
    payload = {
        "items": [{"id": i, "name": "n" * 50} for i in range(30)],
        "meta": {"a": {"b": {"c": {"d": 1}}}},
        "text": "t" * 100,
    }
    out = await governor(tmp_path, max_tokens=100).govern(
        json.dumps(payload), task_id="t", step=1, tool_name="web", taken=frozenset()
    )
    body = out.summary.split("\n[完整输出")[0]
    outline = json.loads(body)
    assert len(outline["items"]) <= 3  # 前 2 项 + 一条省略说明
    assert any("省略" in str(x) for x in outline["items"])
    assert len(outline["text"]) <= 13
    assert outline["meta"]["a"]["b"]  # 深度 3 之内仍展开
    assert "artifact://obs_step_1_web" in out.summary


@pytest.mark.unit
async def test_a_json_array_top_level_is_outlined_too(tmp_path: Path) -> None:
    out = await governor(tmp_path, max_tokens=50).govern(
        json.dumps(list(range(100))), task_id="t", step=1, tool_name="x", taken=frozenset()
    )
    outline = json.loads(out.summary.split("\n[完整输出")[0])
    assert isinstance(outline, list) and len(outline) <= 3


@pytest.mark.unit
async def test_text_that_only_looks_like_json_is_distilled_as_text(tmp_path: Path) -> None:
    broken = "{" + "\n".join(f'"k{i}": {i},' for i in range(100))
    out = await governor(tmp_path, max_tokens=50).govern(
        broken, task_id="t", step=1, tool_name="x", taken=frozenset()
    )
    assert out.truncated is True
    assert "省略" in out.summary


# ==============================================================================
# 豁免
# ==============================================================================


@pytest.mark.unit
async def test_exempt_tools_get_a_wider_limit_but_still_a_limit(tmp_path: Path) -> None:
    g = governor(tmp_path)  # 阈值 200，豁免倍数 4 -> 800
    inside = "s" * 700
    outside = "\n".join("s" * 20 for _ in range(100))
    a = await g.govern(inside, task_id="t", step=1, tool_name="load_skill", taken=frozenset())
    assert a.summary == inside
    b = await g.govern(outside, task_id="t", step=1, tool_name="load_skill", taken=frozenset())
    assert b.truncated is True
    c = await g.govern(inside, task_id="t", step=1, tool_name="bash", taken=frozenset())
    assert c.truncated is True


# ==============================================================================
# 落盘
# ==============================================================================


@pytest.mark.unit
async def test_a_failed_save_leaves_a_summary_without_a_handle(tmp_path: Path) -> None:
    blocker = tmp_path / "artifacts"
    blocker.write_text("我是个文件，不是目录")
    out = await governor(tmp_path).govern(
        numbered(60), task_id="t", step=1, tool_name="bash", taken=frozenset()
    )
    assert out.truncated is True
    assert out.artifact_id is None and out.artifact_path is None
    assert "artifact://" not in out.summary
    assert "省略" in out.summary


@pytest.mark.unit
async def test_a_read_only_directory_also_degrades_quietly(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        out = await governor(tmp_path).govern(
            numbered(60), task_id="t", step=1, tool_name="bash", taken=frozenset()
        )
    finally:
        root.chmod(stat.S_IRWXU)
    assert out.artifact_id is None
    assert out.truncated is True


@pytest.mark.unit
async def test_handles_are_unique_within_a_step(tmp_path: Path) -> None:
    g = governor(tmp_path)
    first = await g.govern(numbered(60), task_id="t", step=2, tool_name="grep", taken=frozenset())
    second = await g.govern(
        numbered(61),
        task_id="t",
        step=2,
        tool_name="grep",
        taken=frozenset({first.artifact_id or ""}),
    )
    assert first.artifact_id == "obs_step_2_grep"
    assert second.artifact_id == "obs_step_2_grep_2"
    assert first.artifact_path != second.artifact_path


@pytest.mark.unit
@pytest.mark.parametrize("name", ["../../evil", "a/b", "srv.tool name", "工具"])
async def test_odd_tool_names_cannot_escape_the_artifact_directory(
    tmp_path: Path, name: str
) -> None:
    out = await governor(tmp_path).govern(
        numbered(60), task_id="t", step=1, tool_name=name, taken=frozenset()
    )
    assert out.artifact_path is not None
    root = (tmp_path / "artifacts").resolve()
    assert Path(out.artifact_path).resolve().is_relative_to(root)  # noqa: ASYNC240


@pytest.mark.unit
async def test_a_hostile_task_id_cannot_escape_either(tmp_path: Path) -> None:
    out = await governor(tmp_path).govern(
        numbered(60), task_id="../../x", step=1, tool_name="bash", taken=frozenset()
    )
    assert out.artifact_path is not None
    root = (tmp_path / "artifacts").resolve()
    assert Path(out.artifact_path).resolve().is_relative_to(root)  # noqa: ASYNC240
