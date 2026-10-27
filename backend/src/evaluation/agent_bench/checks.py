"""任务结束后，由评测端对工作区与交付做机械检查。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from evaluation.agent_bench.cases import Case, Check

_DETAIL_CLIP = 300


@dataclass(frozen=True, slots=True)
class CheckResult:
    kind: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "passed": self.passed, "detail": self.detail}


def _inside(root: Path, rel: str) -> Path | None:
    target = (root / rel).resolve()
    return target if target == root.resolve() or root.resolve() in target.parents else None


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _DETAIL_CLIP else flat[: _DETAIL_CLIP - 1] + "…"


async def run_checks(case: Case, root: Path, delivery: str) -> list[CheckResult]:
    return [await _run_one(check, case, root, delivery) for check in case.checks]


async def _run_one(check: Check, case: Case, root: Path, delivery: str) -> CheckResult:
    kind = check.kind
    if kind.startswith("delivery_"):
        found = check.text.lower() in delivery.lower()
        want = kind == "delivery_contains"
        return CheckResult(
            kind,
            found == want,
            "" if found == want else f"交付里{'没有' if want else '含有'} {check.text!r}",
        )
    if kind == "command_ok":
        return await _command(check, root)

    target = _inside(root, check.path)
    if target is None:
        return CheckResult(kind, False, f"路径越出工作区：{check.path}")
    exists = target.is_file()
    if kind == "file_exists":
        return CheckResult(kind, exists, "" if exists else f"{check.path} 不存在")
    if kind == "file_absent":
        return CheckResult(kind, not exists, "" if not exists else f"{check.path} 不该存在却存在")
    if not exists:
        return CheckResult(kind, kind == "file_not_contains", f"{check.path} 不存在")
    text = target.read_text(encoding="utf-8", errors="replace")
    if kind == "file_contains":
        ok = check.text in text
        return CheckResult(kind, ok, "" if ok else f"{check.path} 里没有 {check.text!r}")
    if kind == "file_not_contains":
        ok = check.text not in text
        return CheckResult(kind, ok, "" if ok else f"{check.path} 里仍有 {check.text!r}")
    baseline = case.files.get(check.path)  # file_unchanged
    if baseline is None:
        return CheckResult(kind, False, f"{check.path} 不在用例的初始文件里，没有基线")
    ok = text == baseline
    return CheckResult(kind, ok, "" if ok else f"{check.path} 被修改了")


async def _command(check: Check, root: Path) -> CheckResult:
    cmd = [
        sys.executable if part == "python" and i == 0 else part for i, part in enumerate(check.cmd)
    ]

    def run() -> CheckResult:
        try:
            done = subprocess.run(
                cmd,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=check.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CheckResult("command_ok", False, f"命令超时（{check.timeout_sec:g} 秒）")
        except OSError as exc:
            return CheckResult("command_ok", False, f"命令无法执行：{exc}")
        if done.returncode != 0:
            return CheckResult(
                "command_ok", False, f"退出码 {done.returncode}：{_clip(done.stdout + done.stderr)}"
            )
        if check.text and check.text not in done.stdout:
            return CheckResult(
                "command_ok", False, f"输出里没有 {check.text!r}：{_clip(done.stdout)}"
            )
        return CheckResult("command_ok", True)

    return await asyncio.to_thread(run)
