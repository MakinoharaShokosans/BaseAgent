"""委派子智能体的报告与引用纪律。设计文档：docs/06_subagents/02_dynamic_delegation.md §5。

每条结论要带 ``Citation``，系统校验 ``ref`` 是否在子智能体**本次真实访问成功**过的资源集合里：
URL 精确命中；文件与产物允许路径分量后缀双向匹配。引用被整条丢弃的结论 ``verified`` 为假，
这个标志由代码置位，模型无法声称。访问集合只采集资源型字段，刻意不采集检索词。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, BeforeValidator, Field

_PATH_KEYS: Final = ("path", "file_path", "filepath", "artifact_id", "url")
_META_KEYS: Final = ("url", "urls", "artifact_id", "artifact_path", "source_url")
_KINDS: Final = ("file", "artifact", "url")
_NOTE: Final = 200


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _dicts(value: object) -> list[object]:
    return [v for v in value if isinstance(v, dict | BaseModel)] if isinstance(value, list) else []


def _strings(value: object) -> list[object]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


Lenient = Annotated[str, BeforeValidator(_text)]


class RawCitation(BaseModel):
    kind: Lenient = ""
    ref: Lenient = ""
    note: Lenient = ""


class RawSubFinding(BaseModel):
    statement: Lenient = ""
    citations: Annotated[list[RawCitation], BeforeValidator(_dicts)] = Field(default_factory=list)


class RawSubagentReport(BaseModel):
    findings: Annotated[list[RawSubFinding], BeforeValidator(_dicts)] = Field(default_factory=list)
    unresolved: Annotated[list[str], BeforeValidator(_strings)] = Field(default_factory=list)
    warnings: Annotated[list[str], BeforeValidator(_strings)] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Citation:
    kind: Literal["file", "artifact", "url"]
    ref: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class SubagentFinding:
    statement: str
    citations: tuple[Citation, ...]
    #: 由代码置位：至少一条引用通过了白名单
    verified: bool
    dropped_citations: int = 0


@dataclass(frozen=True, slots=True)
class SubagentReport:
    status: Literal["success", "partial", "failed"]
    findings: tuple[SubagentFinding, ...] = ()
    unresolved: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    steps_used: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    elapsed_sec: float = 0.0
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DelegateLimits:
    max_findings: int
    max_answer_chars: int
    max_citations: int
    max_report_chars: int


@dataclass(slots=True)
class AccessSet:
    """子智能体本次实际访问**成功**过的资源。

    失败的调用不算（读不存在的路径不能变成“已验证”的引用）；检索词不是资源，所以不采集。
    """

    urls: set[str] = field(default_factory=set[str])
    files: set[str] = field(default_factory=set[str])
    artifacts: set[str] = field(default_factory=set[str])

    def record(self, args: Mapping[str, Any], meta: Mapping[str, Any], *, ok: bool) -> None:
        if not ok:
            return
        for key in _PATH_KEYS:
            self._add(args.get(key), artifact=key == "artifact_id")
        for key in _META_KEYS:
            value = meta.get(key)
            for item in value if isinstance(value, list | tuple) else [value]:
                self._add(item, artifact=key.startswith("artifact"))

    def _add(self, value: object, *, artifact: bool) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        text = value.strip()
        if text.startswith(("http://", "https://")):
            self.urls.add(text)
        elif artifact or text.startswith("artifact://"):
            ref = text.removeprefix("artifact://")
            self.artifacts.add(ref)
            self.artifacts.add(PurePosixPath(ref).name)  # 产物既可以用路径也可以用文件名引用
            self.artifacts.add(PurePosixPath(ref).stem)
        else:
            self.files.add(text)

    def covers(self, kind: str, ref: str) -> bool:
        ref = ref.strip()
        if kind == "url":
            return ref in self.urls
        if kind == "artifact":
            return ref.removeprefix("artifact://") in self.artifacts
        return bool(ref) and any(_same_file(ref, known) for known in self.files)


def _same_file(ref: str, known: str) -> bool:
    """路径分量后缀双向匹配：绝对与相对形态不同也认；只有文件名相同、目录各异的不算同一个。"""
    mine, theirs = PurePosixPath(ref).parts, PurePosixPath(known).parts
    n = min(len(mine), len(theirs))
    if n == 0 or mine[-n:] != theirs[-n:]:
        return False
    if len(mine) == len(theirs):
        return True
    return n >= 2  # 一边只有文件名、另一边带目录：太容易蹭上不相干的路径，不认


def _clip(text: str, limit: int) -> tuple[str, bool]:
    cleaned = text.strip()
    return (cleaned[:limit], True) if len(cleaned) > limit else (cleaned, False)


def build_report(
    raw: RawSubagentReport,
    *,
    access: AccessSet,
    limits: DelegateLimits,
    steps_used: int,
    tool_calls: int,
    total_tokens: int,
    elapsed_sec: float,
    extra_warnings: tuple[str, ...] = (),
) -> SubagentReport:
    truncated = False
    findings: list[SubagentFinding] = []
    for item in raw.findings:
        if len(findings) >= limits.max_findings:
            truncated = True
            break
        statement, cut = _clip(item.statement, limits.max_answer_chars)
        if not statement:
            continue
        truncated = truncated or cut
        kept: list[Citation] = []
        dropped = 0
        for citation in item.citations:
            kind = citation.kind.strip().lower()
            if kind not in _KINDS or not access.covers(kind, citation.ref):
                dropped += 1
                continue
            if len(kept) >= limits.max_citations:
                truncated = True
                continue
            kept.append(Citation(kind, citation.ref.strip(), _clip(citation.note, _NOTE)[0]))  # type: ignore[arg-type]
        findings.append(SubagentFinding(statement, tuple(kept), bool(kept), dropped))
    unresolved = tuple(_clip(u, _NOTE)[0] for u in raw.unresolved[:8] if u.strip())
    status: Literal["success", "partial", "failed"] = "success"
    if not findings:
        status = "failed"
    elif unresolved:
        status = "partial"
    warnings = tuple(
        dict.fromkeys(
            [*extra_warnings, *(_clip(w, _NOTE)[0] for w in raw.warnings[:8] if w.strip())]
        )
    )[:10]
    return SubagentReport(
        status,
        tuple(findings),
        unresolved,
        warnings,
        steps_used,
        tool_calls,
        total_tokens,
        round(elapsed_sec, 2),
        truncated,
    )
