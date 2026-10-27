"""``baseagent-bench``：对一个运行中的 BaseAgent 服务跑评测。默认不运行，只有显式调用才会花 Token。

    baseagent-bench --builtin --repeat 3 --label baseline
    baseagent-bench --cases my_cases.json --only fix_failing_test
    baseagent-bench --compare eval_reports/a.json eval_reports/b.json

服务需要已经启动（``baseagent start``），并配好了模型密钥。评测在临时目录里建工作区，结束后清理。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import httpx

from baseagent_config import env_value, pick_home
from evaluation.agent_bench.cases import BUILTIN, Case, load_cases, select
from evaluation.agent_bench.report import build_report, compare, save, to_markdown
from evaluation.agent_bench.runner import BenchOptions, BenchRunner, CaseResult

DEFAULT_URL = "http://127.0.0.1:11328"
API_PREFIX = "/api/v1"


def _home() -> Path:
    return pick_home()


def read_token(explicit: str | None, token_file: Path | None) -> str:
    """令牌来源：参数 → 环境变量 BASEAGENT_API_TOKEN → 令牌文件。永远不打印。"""
    if explicit:
        return explicit
    if value := (env_value("API_TOKEN") or "").strip():
        return value
    path = token_file or _home() / "api_token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"读不到访问令牌（{path}）：{exc.strerror or exc}") from exc


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="baseagent-bench", description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=env_value("URL") or DEFAULT_URL, help="服务地址")
    p.add_argument("--token", default=None, help="访问令牌（缺省读环境变量或令牌文件）")
    p.add_argument("--token-file", type=Path, default=None)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--builtin", action="store_true", help="跑内置用例（缺省）")
    src.add_argument("--cases", type=Path, help="用例 JSON 文件")
    p.add_argument("--only", default="", help="只跑这些用例 id，逗号分隔")
    p.add_argument("--category", default="", help="只跑这个类别")
    p.add_argument("--repeat", type=int, default=1, help="每个用例重复次数（模型有随机性）")
    p.add_argument("--max-case-tokens", type=int, default=50_000)
    p.add_argument("--max-total-tokens", type=int, default=1_000_000)
    p.add_argument("--timeout", type=float, default=600.0, help="单用例超时秒数")
    p.add_argument("--out", type=Path, default=Path("eval_reports"))
    p.add_argument("--label", default="bench")
    p.add_argument("--keep", action="store_true", help="保留评测创建的工作区与会话")
    p.add_argument("--list", action="store_true", help="只列出将要运行的用例")
    p.add_argument("--compare", nargs=2, type=Path, metavar=("旧报告", "新报告"))
    return p


def _announce(result: CaseResult) -> None:
    mark = "PASS" if result.success else "FAIL"
    why = result.note or ("" if result.success else result.termination_reason)
    print(
        f"[{mark}] {result.case_id}#{result.repeat}  {result.status}  "
        f"步数 {result.steps}  Token {result.total_tokens}  {result.wall_sec:.0f}s  {why}".rstrip(),
        flush=True,
    )


async def run(args: argparse.Namespace, cases: Sequence[Case]) -> int:
    token = read_token(args.token, args.token_file)
    options = BenchOptions(
        default_timeout_sec=args.timeout,
        default_max_tokens=args.max_case_tokens,
        max_total_tokens=args.max_total_tokens,
        cleanup=not args.keep,
    )
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=args.url.rstrip("/") + API_PREFIX, headers=headers, timeout=60.0
    ) as client:
        try:
            health = await client.get("/health/dependencies")
        except httpx.HTTPError as exc:
            print(
                f"连不上 {args.url}：{type(exc).__name__}。先用 `baseagent start` 启动服务。",
                file=sys.stderr,
            )
            return 2
        if health.status_code == 401:
            print("访问令牌被拒绝，检查 --token / BASEAGENT_API_TOKEN。", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory(prefix="baseagent-bench-") as tmp:
            runner = BenchRunner(client, Path(tmp), options=options)
            results = await runner.run(cases, repeat=args.repeat, progress=_announce)
    report = build_report(results, {"label": args.label, "url": args.url, "repeat": args.repeat})
    json_path, md_path = save(report, args.out, args.label)
    print("\n" + to_markdown(report))
    print(f"报告：{md_path}\nJSON：{json_path}")
    return 0 if report["overall"]["success"] == report["overall"]["ran"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.compare:
        old, new = (json.loads(p.read_text(encoding="utf-8")) for p in args.compare)
        print(compare(old, new))
        return 0
    cases = load_cases(args.cases) if args.cases else list(BUILTIN)
    try:
        chosen = select(cases, [s for s in args.only.split(",") if s], args.category)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not chosen:
        print("没有符合条件的用例。", file=sys.stderr)
        return 2
    if args.list:
        for c in chosen:
            print(f"{c.id:28} {c.category:10} {c.permission_level}")
        return 0
    return asyncio.run(run(args, chosen))


if __name__ == "__main__":
    raise SystemExit(main())
