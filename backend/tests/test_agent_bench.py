"""评测框架：用例、检查、指标、报告与运行器。出处：docs/11_testing/01_strategy.md §5.5。

运行器用真实的 API 应用（ASGI，假阶段）验证：提交、轮询、审批、预算取消、清理。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from api_support import HEADERS, ApiRig, build_rig
from evaluation.agent_bench import (
    BUILTIN,
    BenchOptions,
    BenchRunner,
    Case,
    Check,
    build_report,
    compare,
    load_cases,
    to_markdown,
)
from evaluation.agent_bench.cases import case_from_dict, select
from evaluation.agent_bench.checks import run_checks
from evaluation.agent_bench.cli import main, parser, read_token
from evaluation.agent_bench.metrics import CaseMetrics, parse_sse, summarize_events
from evaluation.agent_bench.report import median, percentile, save
from evaluation.agent_bench.runner import CaseResult

# ==============================================================================
# 用例
# ==============================================================================


@pytest.mark.unit
def test_the_builtin_cases_are_valid_unique_and_cover_the_categories() -> None:
    ids = [c.id for c in BUILTIN]
    assert len(ids) == len(set(ids))
    assert {c.category for c in BUILTIN} >= {
        "readonly",
        "write",
        "bugfix",
        "refactor",
        "security",
        "feature",
        "debug",
        "clarify",
    }
    assert all(c.checks and c.goal for c in BUILTIN)


#: 做事类用例：初始工作区必须通不过检查，否则它什么都不测
_MUST_START_FAILING = ("write", "bugfix", "refactor", "edit", "feature", "debug")


@pytest.mark.unit
@pytest.mark.parametrize(
    "case", [c for c in BUILTIN if c.category in _MUST_START_FAILING], ids=lambda c: c.id
)
async def test_work_cases_do_not_pass_before_the_agent_does_anything(
    case: Case, tmp_path: Path
) -> None:
    for rel, content in case.files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content, encoding="utf-8")
    results = await run_checks(case, tmp_path, "")
    assert not all(r.passed for r in results), f"{case.id} 的检查在没动手前就全部通过了"


@pytest.mark.unit
async def test_the_reference_solutions_make_the_new_cases_pass(tmp_path: Path) -> None:
    """用例本身要可解：写一份参考解答，检查应当全部通过。"""
    solutions = {
        "implement_slugify": {
            "textutil.py": (
                "import re\n\n\ndef slugify(text):\n"
                "    words = [re.sub(r'[^a-z0-9]', '', w) for w in text.lower().split()]\n"
                "    return '-'.join(w for w in words if w)\n"
            )
        },
        "two_bugs_one_module": {
            "stats.py": (
                "def mean(xs):\n    return sum(xs) / len(xs)\n\n\n"
                "def median(xs):\n    s = sorted(xs)\n    mid = len(s) // 2\n"
                "    if len(s) % 2 == 0:\n"
                "        return (s[mid - 1] + s[mid]) / 2\n"
                "    return s[mid]\n"
            )
        },
        "trace_across_modules": {"config.py": "RATES = {'gold': 0.2, 'silver': 0.1}\n"},
    }
    by_id = {c.id: c for c in BUILTIN}
    for case_id, fixes in solutions.items():
        root = tmp_path / case_id
        root.mkdir()
        for rel, content in {**by_id[case_id].files, **fixes}.items():
            (root / rel).write_text(content, encoding="utf-8")
        results = await run_checks(by_id[case_id], root, "")
        assert all(r.passed for r in results), (
            case_id,
            [r.detail for r in results if not r.passed],
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "build",
    [
        lambda: Check("nonsense"),
        lambda: Check("file_exists"),
        lambda: Check("file_contains", path="a"),
        lambda: Check("command_ok"),
        lambda: Check("delivery_contains"),
        lambda: Case("bad id!", "c", "g"),
        lambda: Case("ok", "c", "g", permission_level="root"),
        lambda: Case("ok", "c", "g", expect_status=("maybe",)),
        lambda: Case("ok", "c", "g", expect_status=()),
        lambda: Case("ok", "c", "g", files={"../escape": "x"}),
        lambda: Case("ok", "c", "g", files={"/abs": "x"}),
    ],
)
def test_invalid_cases_and_checks_are_rejected_when_built(build: Any) -> None:
    with pytest.raises(ValueError):
        build()


@pytest.mark.unit
def test_cases_load_from_json_and_ids_must_be_unique(tmp_path: Path) -> None:
    item = {
        "id": "a",
        "category": "x",
        "goal": "g",
        "files": {"f.txt": "1"},
        "checks": [{"kind": "file_contains", "path": "f.txt", "text": "1"}],
        "permission_level": "read_only",
        "max_tokens": 1000,
    }
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [item]}), encoding="utf-8")
    [case] = load_cases(path)
    assert case.permission_level == "read_only" and case.max_tokens == 1000
    assert case.checks[0].kind == "file_contains" and case_from_dict(item) == case
    path.write_text(json.dumps([item, item]), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_cases(path)


@pytest.mark.unit
def test_select_filters_by_id_and_category_and_reports_unknown_ids() -> None:
    assert [c.id for c in select(BUILTIN, ["create_script"])] == ["create_script"]
    assert {c.category for c in select(BUILTIN, category="security")} == {"security"}
    with pytest.raises(ValueError, match="nope"):
        select(BUILTIN, ["nope"])


# ==============================================================================
# 检查
# ==============================================================================


def case_with(*checks: Check, **kw: Any) -> Case:
    return Case("c", "t", "g", checks=checks, **kw)


@pytest.mark.unit
async def test_file_checks_judge_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello world")
    case = case_with(
        Check("file_exists", path="a.txt"),
        Check("file_absent", path="gone.txt"),
        Check("file_contains", path="a.txt", text="world"),
        Check("file_not_contains", path="a.txt", text="PWNED"),
        Check("file_exists", path="missing.txt"),
        Check("file_absent", path="a.txt"),
        Check("file_contains", path="a.txt", text="nope"),
        Check("file_not_contains", path="a.txt", text="hello"),
    )
    passed = [r.passed for r in await run_checks(case, tmp_path, "")]
    assert passed == [True, True, True, True, False, False, False, False]


@pytest.mark.unit
async def test_file_unchanged_compares_with_the_initial_content(tmp_path: Path) -> None:
    (tmp_path / "t.py").write_text("v1")
    case = case_with(Check("file_unchanged", path="t.py"), files={"t.py": "v1"})
    assert (await run_checks(case, tmp_path, ""))[0].passed
    (tmp_path / "t.py").write_text("v2")
    changed = (await run_checks(case, tmp_path, ""))[0]
    assert not changed.passed and "修改" in changed.detail
    nobaseline = case_with(Check("file_unchanged", path="t.py"))
    assert not (await run_checks(nobaseline, tmp_path, ""))[0].passed


@pytest.mark.unit
@pytest.mark.security
async def test_checks_cannot_read_outside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("s")
    result = (await run_checks(case_with(Check("file_exists", path="../secret.txt")), root, ""))[0]
    assert not result.passed and "越出" in result.detail


@pytest.mark.unit
async def test_delivery_checks_are_case_insensitive() -> None:
    case = case_with(
        Check("delivery_contains", text="Main.PY"), Check("delivery_not_contains", text="error")
    )
    assert all(r.passed for r in await run_checks(case, Path("."), "入口是 main.py"))
    assert not any(r.passed for r in await run_checks(case, Path("."), "ERROR 没有"))


@pytest.mark.unit
async def test_command_checks_run_in_the_workspace_with_exit_code_and_output(
    tmp_path: Path,
) -> None:
    (tmp_path / "hello.py").write_text("print('Hello')")
    case = case_with(
        Check("command_ok", cmd=("python", "hello.py"), text="Hello"),
        Check("command_ok", cmd=("python", "hello.py"), text="Bye"),
        Check("command_ok", cmd=("python", "-c", "import sys;sys.exit(3)")),
        Check("command_ok", cmd=("python", "-c", "import time;time.sleep(5)"), timeout_sec=0.3),
        Check("command_ok", cmd=("/nonexistent/binary",)),
    )
    results = await run_checks(case, tmp_path, "")
    assert [r.passed for r in results] == [True, False, False, False, False]
    assert "退出码 3" in results[2].detail and "超时" in results[3].detail


# ==============================================================================
# 指标
# ==============================================================================

SSE = (
    'id: 1\nevent: task.started\ndata: {"event":"task.started","task_id":"t"}\n\n'
    'event: heartbeat\ndata: {"task_id":"t"}\n\n'
    'id: 2\nevent: node.finished\ndata: {"node":"planner","stage":1}\n\n'
    'id: 3\nevent: llm.call\ndata: {"node":"planner","tokens":100,"prompt_tokens":90,'
    '"completion_tokens":10,"cache_hit_tokens":60,"cache_miss_tokens":30,"duration_ms":50,'
    '"attempts":2,"outcome":"ok"}\n\n'
    'id: 4\nevent: tool.call\ndata: {"tool":"bash"}\n\n'
    'id: 5\nevent: tool.observed\ndata: {"ok":false}\n\n'
    "id: 6\nevent: guard.loop_blocked\ndata: {}\n\n"
    "id: 7\nevent: broken\ndata: {not json\n\n"
    'id: 8\nevent: task.finished\ndata: {"status":"succeeded","delivery":"好"}\n\n'
)


@pytest.mark.unit
def test_sse_parsing_skips_heartbeats_and_garbage() -> None:
    events = parse_sse(SSE)
    assert [e["event"] for e in events] == [
        "task.started",
        "node.finished",
        "llm.call",
        "tool.call",
        "tool.observed",
        "guard.loop_blocked",
        "task.finished",
    ]


@pytest.mark.unit
def test_metrics_are_computed_from_events() -> None:
    m = summarize_events(parse_sse(SSE))
    assert m.stages == {"planner": 1} and m.planner_calls == 1
    assert (m.llm_calls, m.llm_retries, m.llm_ms) == (1, 1, 50)
    assert m.tokens_by_node == {"planner": 100} and m.prompt_tokens == 90
    assert m.cache_hit_rate == pytest.approx(60 / 90)
    assert (m.tool_calls, m.tool_failures, m.loop_blocked) == (1, 1, 1)
    assert CaseMetrics().cache_hit_rate is None  # 没有数据就是没有，不是 0


# ==============================================================================
# 报告
# ==============================================================================


def result(
    case: str, ok: bool, *, category: str = "bugfix", steps: int = 4, tokens: int = 1000
) -> CaseResult:
    r = CaseResult(case, category, 0, ok, "succeeded" if ok else "terminated")
    r.steps, r.total_tokens, r.wall_sec = steps, tokens, 2.0
    r.metrics = summarize_events(parse_sse(SSE))
    return r


@pytest.mark.unit
def test_percentiles_use_nearest_rank() -> None:
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert (percentile(values, 0.9), median(values), percentile([], 0.9)) == (9.0, 5.0, 0.0)
    assert percentile([7], 0.9) == 7.0


@pytest.mark.unit
def test_the_report_groups_by_category_and_ignores_skipped_cases() -> None:
    skipped = CaseResult("s", "bugfix", 0, False, "skipped")
    report = build_report(
        [result("a", True), result("b", False), result("c", True, category="write"), skipped],
        {"label": "x"},
    )
    assert report["overall"]["ran"] == 3 and report["overall"]["success"] == 2
    assert report["by_category"]["bugfix"]["success_rate"] == 0.5
    assert report["by_category"]["write"]["steps_median"] == 4
    assert report["overall"]["token_share_by_node"] == {"planner": 1.0}
    assert report["overall"]["cache_hit_rate"] == pytest.approx(60 / 90)
    text = to_markdown(report)
    assert "成功率 67%" in text and "| bugfix |" in text and "用例明细" in text


@pytest.mark.unit
def test_two_reports_compare_with_deltas_and_flipped_cases(tmp_path: Path) -> None:
    old = build_report([result("a", True, tokens=2000), result("b", False, tokens=2000)])
    new = build_report([result("a", True, tokens=1000), result("b", True)])
    text = compare(old, new)
    assert "Token 中位" in text and "-50%" in text and "+50pp" in text
    assert "- b：0/1 → 1/1" in text and "- a：" not in text
    json_path, md_path = save(new, tmp_path, "my label!")
    assert json_path.suffix == ".json" and md_path.read_text().startswith("# Agent 评测报告")
    assert json.loads(json_path.read_text())["schema"] == 1


# ==============================================================================
# 运行器（真实 API 应用 + 假阶段）
# ==============================================================================


@pytest.fixture
async def bench(tmp_path: Path) -> AsyncIterator[tuple[ApiRig, httpx.AsyncClient, Path]]:
    async for rig in build_rig(tmp_path / "rig"):
        transport = httpx.ASGITransport(app=rig.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000/api/v1", headers=HEADERS
        ) as client:
            work = tmp_path / "bench"
            work.mkdir()
            yield rig, client, work


FAST = BenchOptions(poll_sec=0.02, default_timeout_sec=10)


@pytest.mark.integration
async def test_a_case_runs_end_to_end_and_its_checks_are_applied(bench: Any) -> None:
    _rig, client, work = bench
    case = Case(
        "ok",
        "t",
        "做点什么",
        files={"seed.txt": "s"},
        checks=(Check("file_exists", path="seed.txt"), Check("file_absent", path="x")),
    )
    runner = BenchRunner(client, work, options=FAST)
    [r] = await runner.run([case])
    assert r.success and r.status == "succeeded" and r.steps >= 1 and r.task_id
    assert [c.passed for c in r.checks] == [True, True]
    assert r.metrics.stages.get("executor", 0) >= 1  # 指标来自服务端事件
    assert runner.spent_tokens == r.total_tokens


@pytest.mark.integration
async def test_a_failed_check_makes_the_case_fail_even_if_the_task_succeeded(bench: Any) -> None:
    _, client, work = bench
    case = Case("bad", "t", "g", checks=(Check("file_exists", path="never.txt"),))
    [r] = await BenchRunner(client, work, options=FAST).run([case])
    assert r.status == "succeeded" and not r.success
    assert "never.txt" in r.checks[0].detail


@pytest.mark.integration
async def test_an_unexpected_final_status_fails_the_case(bench: Any) -> None:
    rig, client, work = bench
    rig.world.terminate = True
    case = Case("t1", "t", "g")
    [r] = await BenchRunner(client, work, options=FAST).run([case])
    assert r.status == "terminated" and not r.success and "步数" in r.termination_reason
    allowed = Case("t2", "t", "g", expect_status=("terminated",))
    [ok] = await BenchRunner(client, work, options=FAST).run([allowed])
    assert ok.success


@pytest.mark.integration
async def test_approvals_are_answered_per_case(bench: Any) -> None:
    rig, client, work = bench
    rig.world.approval = True
    approve = Case("yes", "t", "g", approve=True)
    deny = Case("no", "t", "g", approve=False)
    keep = BenchOptions(poll_sec=0.02, default_timeout_sec=10, cleanup=False)
    results = await BenchRunner(client, work, options=keep).run([approve, deny])
    assert [r.metrics.approvals for r in results] == [1, 1]
    assert all(r.status == "succeeded" for r in results)
    sent = {r.case_id: r.task_id for r in results}
    events = (await client.get(f"/tasks/{sent['yes']}/stream?after=0")).text
    assert "task.approved" in events and "task.rejected" not in events
    events = (await client.get(f"/tasks/{sent['no']}/stream?after=0")).text
    assert "task.rejected" in events


@pytest.mark.integration
async def test_a_stuck_case_is_cancelled_at_the_timeout(bench: Any) -> None:
    rig, client, work = bench
    rig.world.gate = asyncio.Event()  # 执行阶段永远不放行
    case = Case("slow", "t", "g", timeout_sec=0.3)
    [r] = await BenchRunner(client, work, options=FAST).run([case])
    assert r.status == "cancelled" and not r.success
    assert "评测超时" in r.termination_reason
    rig.world.gate.set()


@pytest.mark.integration
async def test_the_total_token_budget_stops_new_cases(bench: Any) -> None:
    _, client, work = bench
    options = BenchOptions(poll_sec=0.02, default_timeout_sec=10, max_total_tokens=1)
    runner = BenchRunner(client, work, options=options)
    runner.spent_tokens = 5
    [r] = await runner.run([Case("late", "t", "g")])
    assert r.status == "skipped" and not r.success and "预算" in r.note


@pytest.mark.integration
async def test_repeats_and_progress_callbacks(bench: Any) -> None:
    _, client, work = bench
    seen: list[str] = []

    async def progress(r: CaseResult) -> None:
        seen.append(f"{r.case_id}#{r.repeat}")

    results = await BenchRunner(client, work, options=FAST).run(
        [Case("rep", "t", "g")], repeat=2, progress=progress
    )
    assert seen == ["rep#0", "rep#1"] and [r.repeat for r in results] == [0, 1]


@pytest.mark.integration
async def test_workspaces_are_cleaned_up_unless_asked_to_keep(bench: Any) -> None:
    _, client, work = bench
    await BenchRunner(client, work, options=FAST).run([Case("a", "t", "g")])
    assert (await client.get("/workspaces")).json() == []
    keep = BenchOptions(poll_sec=0.02, default_timeout_sec=10, cleanup=False)
    await BenchRunner(client, work, options=keep).run([Case("b", "t", "g")])
    assert len((await client.get("/workspaces")).json()) == 1


@pytest.mark.integration
async def test_a_rejected_request_is_reported_not_raised(bench: Any) -> None:
    _, client, work = bench
    forbidden = Case("root", "t", "g")
    bad = httpx.AsyncClient(
        transport=client._transport, base_url="http://127.0.0.1:8000/api/v1", headers={}
    )
    async with bad:
        [r] = await BenchRunner(bad, work, options=FAST).run([forbidden])
    assert not r.success and r.status == "error" and "401" in r.note


# ==============================================================================
# 命令行
# ==============================================================================


@pytest.mark.unit
def test_the_cli_lists_cases_and_filters(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--list", "--category", "security"]) == 0
    out = capsys.readouterr().out
    assert "injection_in_file" in out and "create_script" not in out
    assert main(["--list", "--only", "nope"]) == 2


@pytest.mark.unit
def test_the_cli_compares_two_report_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(build_report([result("x", False)])), encoding="utf-8")
    b.write_text(json.dumps(build_report([result("x", True)])), encoding="utf-8")
    assert main(["--compare", str(a), str(b)]) == 0
    assert "- x：0/1 → 1/1" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.security
def test_the_token_comes_from_argument_then_env_then_file_and_is_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    file = tmp_path / "tok"
    file.write_text("from-file\n")
    monkeypatch.delenv("AEGIS_API_TOKEN", raising=False)
    assert read_token(None, file) == "from-file"
    monkeypatch.setenv("AEGIS_API_TOKEN", "from-env")
    assert read_token(None, file) == "from-env"
    assert read_token("explicit", file) == "explicit"
    monkeypatch.delenv("AEGIS_API_TOKEN")
    with pytest.raises(SystemExit) as exc:
        read_token(None, tmp_path / "missing")
    assert "from-file" not in str(exc.value)
    assert capsys.readouterr().out == ""
    assert parser().parse_args(["--repeat", "3"]).repeat == 3


@pytest.mark.unit
def test_the_cli_exits_with_a_hint_when_the_service_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AEGIS_API_TOKEN", "t")
    code = main(["--url", "http://127.0.0.1:9", "--only", "create_script", "--out", str(tmp_path)])
    assert code == 2 and "aegis start" in capsys.readouterr().err
