"""评测框架：用真实任务衡量 Agent，输出可跨版本对比的统计报告。

黑盒：只通过公开的 HTTP API 提交任务、读事件，所以评测的就是用户实际会用到的那一套。
默认不运行，用 ``baseagent-bench`` 显式触发。设计文档：docs/11_testing/01_strategy.md §5.5。
"""

from evaluation.agent_bench.cases import BUILTIN, Case, Check, load_cases
from evaluation.agent_bench.report import build_report, compare, to_markdown
from evaluation.agent_bench.runner import BenchOptions, BenchRunner, CaseResult

__all__ = [
    "BUILTIN",
    "BenchOptions",
    "BenchRunner",
    "Case",
    "CaseResult",
    "Check",
    "build_report",
    "compare",
    "load_cases",
    "to_markdown",
]
