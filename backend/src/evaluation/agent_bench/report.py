"""汇总与报告：Markdown 加 JSON，可跨版本对比。"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evaluation.agent_bench.runner import CaseResult

SCHEMA = 1


def percentile(values: Sequence[float], q: float) -> float:
    """最近邻（nearest-rank）百分位；空序列为 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return float(ordered[rank - 1])


def median(values: Sequence[float]) -> float:
    return percentile(values, 0.5)


def _group(results: Iterable[CaseResult]) -> dict[str, list[CaseResult]]:
    groups: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        groups[r.category].append(r)
    return dict(sorted(groups.items()))


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _block(items: Sequence[CaseResult]) -> dict[str, Any]:
    ran = [r for r in items if r.status != "skipped"]
    steps = [float(r.steps) for r in ran]
    tokens = [float(r.total_tokens) for r in ran]
    wall = [r.wall_sec for r in ran]
    hit = sum(r.metrics.cache_hit_tokens for r in ran)
    miss = sum(r.metrics.cache_miss_tokens for r in ran)
    by_node: dict[str, int] = defaultdict(int)
    for r in ran:
        for node, count in r.metrics.tokens_by_node.items():
            by_node[node] += count
    llm_total = sum(by_node.values())
    calls = sum(r.metrics.llm_calls for r in ran)
    return {
        "cases": len(items),
        "ran": len(ran),
        "success": sum(1 for r in ran if r.success),
        "success_rate": _rate(sum(1 for r in ran if r.success), len(ran)),
        "steps_median": median(steps),
        "steps_p90": percentile(steps, 0.9),
        "tokens_median": median(tokens),
        "tokens_p90": percentile(tokens, 0.9),
        "tokens_total": int(sum(tokens)),
        "wall_median_sec": round(median(wall), 2),
        "wall_p90_sec": round(percentile(wall, 0.9), 2),
        "llm_calls": calls,
        "token_share_by_node": {
            n: round(_rate(t, llm_total), 3) for n, t in sorted(by_node.items())
        },
        "cache_hit_rate": _rate(hit, hit + miss) if hit + miss else None,
        "tool_calls": sum(r.metrics.tool_calls for r in ran),
        "tool_failure_rate": _rate(
            sum(r.metrics.tool_failures for r in ran), sum(r.metrics.tool_calls for r in ran)
        ),
        "degraded_outputs": sum(r.metrics.degraded_outputs for r in ran),
        "loop_blocked": sum(r.metrics.loop_blocked for r in ran),
        "error_notices": sum(r.metrics.error_notices for r in ran),
        "approvals": sum(r.metrics.approvals for r in ran),
        "llm_retries": sum(r.metrics.llm_retries for r in ran),
        "llm_errors": sum(r.metrics.llm_errors for r in ran),
    }


def build_report(
    results: Sequence[CaseResult], meta: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "meta": {"created_at": time.time(), **(meta or {})},
        "overall": _block(results),
        "by_category": {cat: _block(items) for cat, items in _group(results).items()},
        "results": [r.to_dict() for r in results],
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def to_markdown(report: Mapping[str, Any]) -> str:
    overall = report["overall"]
    meta = report.get("meta", {})
    lines = ["# Agent 评测报告", ""]
    for key in ("label", "model", "url", "repeat"):
        if meta.get(key) not in (None, ""):
            lines.append(f"- {key}：{meta[key]}")
    lines += [
        "",
        f"**成功率 {_pct(overall['success_rate'])}**（{overall['success']}/{overall['ran']}），"
        f"Token 合计 {overall['tokens_total']}，缓存命中率 {_pct(overall['cache_hit_rate'])}，"
        f"模型调用 {overall['llm_calls']}",
        "",
        "## 按类别",
        "",
        "| 类别 | 成功 | 步数 中位/P90 | Token 中位/P90 | 耗时 中位/P90 "
        "| 工具失败率 | 拦截/出错提醒 |",
        "|---|---|---|---|---|---|---|",
    ]
    for cat, b in report["by_category"].items():
        lines.append(
            f"| {cat} | {b['success']}/{b['ran']} ({_pct(b['success_rate'])}) "
            f"| {b['steps_median']:g}/{b['steps_p90']:g} "
            f"| {b['tokens_median']:g}/{b['tokens_p90']:g} "
            f"| {b['wall_median_sec']:g}s/{b['wall_p90_sec']:g}s "
            f"| {_pct(b['tool_failure_rate'])} | {b['loop_blocked']}/{b['error_notices']} |"
        )
    shares = overall["token_share_by_node"]
    if shares:
        lines += ["", "## 各调用用途 Token 占比", ""]
        lines += [f"- {node}：{_pct(share)}" for node, share in shares.items()]
    lines += [
        "",
        "## 用例明细",
        "",
        "| 用例 | 次 | 结果 | 终态 | 步数 | Token | 未通过的检查 / 备注 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["results"]:
        bad = [f"{c['kind']}：{c['detail']}" for c in r["checks"] if not c["passed"]]
        extra = "；".join(
            [
                *bad,
                *([r["note"]] if r["note"] else []),
                *(
                    [r["termination_reason"]]
                    if r["status"] != "succeeded" and r["termination_reason"]
                    else []
                ),
            ]
        )
        lines.append(
            f"| {r['case_id']} | {r['repeat']} | {'✅' if r['success'] else '❌'} | {r['status']} "
            f"| {r['steps']} | {r['total_tokens']} | {extra.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def save(report: Mapping[str, Any], directory: Path, label: str = "bench") -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label) or "bench"
    base = directory / f"{safe}-{stamp}"
    json_path, md_path = base.with_suffix(".json"), base.with_suffix(".md")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return json_path, md_path


_COMPARED = (
    ("success_rate", "成功率", True),
    ("steps_median", "步数中位", False),
    ("tokens_median", "Token 中位", False),
    ("tokens_total", "Token 合计", False),
    ("wall_median_sec", "耗时中位(s)", False),
    ("llm_calls", "模型调用", False),
    ("cache_hit_rate", "缓存命中率", True),
    ("tool_failure_rate", "工具失败率", False),
)


def compare(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """两份报告的总体指标对比，并列出成功与否发生变化的用例。"""
    a, b = old["overall"], new["overall"]
    lines = ["# 评测对比", "", "| 指标 | 旧 | 新 | 变化 |", "|---|---|---|---|"]
    for key, label, is_rate in _COMPARED:
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            lines.append(f"| {label} | {_fmt(x, is_rate)} | {_fmt(y, is_rate)} | — |")
            continue
        delta = y - x
        change = (
            f"{delta * 100:+.0f}pp"
            if is_rate
            else (f"{delta:+g}" if x == 0 else f"{(delta / x) * 100:+.0f}%")
        )
        lines.append(f"| {label} | {_fmt(x, is_rate)} | {_fmt(y, is_rate)} | {change} |")
    old_ok = _outcomes(old)
    new_ok = _outcomes(new)
    flips = [
        f"- {case}：{_rate_text(old_ok[case])} → {_rate_text(new_ok[case])}"
        for case in sorted(old_ok.keys() & new_ok.keys())
        if old_ok[case] != new_ok[case]
    ]
    lines += ["", "## 成功率发生变化的用例", "", *(flips or ["- 没有"])]
    return "\n".join(lines) + "\n"


def _fmt(x: float | None, is_rate: bool) -> str:
    if x is None:
        return "—"
    return _pct(x) if is_rate else f"{x:g}"


def _outcomes(report: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in report["results"]:
        if r["status"] == "skipped":
            continue
        out[r["case_id"]][1] += 1
        out[r["case_id"]][0] += 1 if r["success"] else 0
    return {k: (v[0], v[1]) for k, v in out.items()}


def _rate_text(pair: tuple[int, int]) -> str:
    return f"{pair[0]}/{pair[1]}"
