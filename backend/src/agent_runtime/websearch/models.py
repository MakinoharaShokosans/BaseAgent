"""研究报告的强类型契约与净化通道。设计文档：docs/04_tools/07_websearch_tool.md §5。

子模型的任何输出都必须先经 ``build_report`` 才可能被渲染进主上下文。四道结构性约束：
引用的 URL 必须来自本轮真实抓取集合；版本号必须匹配正则；长度有硬上限；
``display_only`` 由契约常量给出，模型无法置为 ``False``。
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Annotated, Final, Literal

from pydantic import BaseModel, BeforeValidator, Field

from agent_runtime.guardrails import scan_injection

VERSION_PATTERN: Final = re.compile(r"^[0-9]+(\.[0-9]+)*([-.+][0-9A-Za-z.\-]+)*$")
COMPONENT_PATTERN: Final = re.compile(r"^[A-Za-z0-9_.\-@/+ ]{1,60}$")
LANGUAGES: Final = frozenset(
    {
        "python",
        "javascript",
        "typescript",
        "bash",
        "shell",
        "json",
        "yaml",
        "toml",
        "sql",
        "go",
        "rust",
        "java",
        "c",
        "cpp",
        "csharp",
        "ruby",
        "php",
        "html",
        "css",
        "markdown",
        "text",
    }
)
CONFIDENCE: Final = ("high", "medium", "low")
_MAX_WARNINGS: Final = 10
_MAX_UNRESOLVED: Final = 8
_NOTE_CHARS: Final = 200


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _dicts(value: object) -> list[object]:
    """子模型的输出不可信：列表里不是对象的条目直接丢掉，而不是让整份报告解析失败。"""
    return [v for v in value if isinstance(v, dict | BaseModel)] if isinstance(value, list) else []


def _strings(value: object) -> list[object]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


Lenient = Annotated[str, BeforeValidator(_text)]


# ==============================================================================
# 子模型的输出（不可信，宽松解析，之后统一净化）
# ==============================================================================


class QueryPlan(BaseModel):
    queries: Annotated[list[str], BeforeValidator(_strings)] = Field(default_factory=list)
    enough: bool = False


class RawFinding(BaseModel):
    question: Lenient = ""
    answer: Lenient = ""
    confidence: Lenient = "low"
    sources: Annotated[list[str], BeforeValidator(_strings)] = Field(default_factory=list)


class RawVersionFact(BaseModel):
    component: Lenient = ""
    version: Lenient = ""
    source_url: Lenient = ""


class RawCodeExample(BaseModel):
    language: Lenient = ""
    code: Lenient = ""
    source_url: Lenient = ""


class RawReport(BaseModel):
    findings: Annotated[list[RawFinding], BeforeValidator(_dicts)] = Field(default_factory=list)
    version_facts: Annotated[list[RawVersionFact], BeforeValidator(_dicts)] = Field(
        default_factory=list
    )
    code_examples: Annotated[list[RawCodeExample], BeforeValidator(_dicts)] = Field(
        default_factory=list
    )
    unresolved: Annotated[list[str], BeforeValidator(_strings)] = Field(default_factory=list)
    warnings: Annotated[list[str], BeforeValidator(_strings)] = Field(default_factory=list)


# ==============================================================================
# 进入主上下文的报告
# ==============================================================================


@dataclass(frozen=True, slots=True)
class Finding:
    question: str
    answer: str
    confidence: Literal["high", "medium", "low"]
    sources: tuple[str, ...]
    #: 答案命中了注入样态：渲染时标注
    flagged: bool = False


@dataclass(frozen=True, slots=True)
class VersionFact:
    component: str
    version: str
    source_url: str


@dataclass(frozen=True, slots=True)
class CodeExample:
    language: str
    code: str
    source_url: str
    truncated: bool = False
    #: 恒为真，由契约常量给出：外部代码永远不会被标记为“已验证可执行”
    display_only: Literal[True] = True


@dataclass(frozen=True, slots=True)
class WebSearchSource:
    url: str
    title: str
    status: str
    artifact_id: str = ""


@dataclass(frozen=True, slots=True)
class WebSearchReport:
    findings: tuple[Finding, ...] = ()
    version_facts: tuple[VersionFact, ...] = ()
    code_examples: tuple[CodeExample, ...] = ()
    sources: tuple[WebSearchSource, ...] = ()
    unresolved: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    rounds_used: int = 0
    total_tokens: int = 0
    elapsed_sec: float = 0.0
    truncated: bool = False

    @property
    def empty(self) -> bool:
        return not (self.findings or self.version_facts or self.code_examples)


@dataclass(frozen=True, slots=True)
class ReportLimits:
    max_findings: int
    max_answer_chars: int
    max_code_examples: int
    max_code_chars: int
    max_report_chars: int


def _clip(text: str, limit: int) -> tuple[str, bool]:
    cleaned = text.strip()
    return (cleaned[:limit], True) if len(cleaned) > limit else (cleaned, False)


def build_report(
    raw: RawReport,
    *,
    fetched: Collection[str],
    sources: tuple[WebSearchSource, ...],
    limits: ReportLimits,
    rounds_used: int,
    total_tokens: int,
    elapsed_sec: float,
    extra_warnings: tuple[str, ...] = (),
) -> WebSearchReport:
    """净化通道。``fetched``：本轮真实抓取过的 URL；其余引用一律丢弃。"""
    allowed = set(fetched)
    truncated = False
    warnings: list[str] = [w for w in extra_warnings]

    findings: list[Finding] = []
    for item in raw.findings:
        if len(findings) >= limits.max_findings:
            truncated = True
            break
        answer, cut = _clip(item.answer, limits.max_answer_chars)
        if not answer:
            continue
        truncated = truncated or cut
        question, _ = _clip(item.question, _NOTE_CHARS)
        confidence = item.confidence if item.confidence in CONFIDENCE else "low"
        cited = tuple(dict.fromkeys(u for u in item.sources if isinstance(u, str) and u in allowed))
        flagged = bool(scan_injection(answer))
        if flagged:
            warnings.append("某条结论的文字带有疑似指令注入的样态，已标注")
        findings.append(Finding(question, answer, confidence, cited, flagged))  # type: ignore[arg-type]

    facts: list[VersionFact] = []
    for fact in raw.version_facts:
        if (
            fact.source_url in allowed
            and VERSION_PATTERN.match(fact.version.strip())
            and COMPONENT_PATTERN.match(fact.component.strip())
        ):
            facts.append(VersionFact(fact.component.strip(), fact.version.strip(), fact.source_url))

    examples: list[CodeExample] = []
    for example in raw.code_examples:
        language = example.language.strip().lower()
        if len(examples) >= limits.max_code_examples:
            truncated = True
            break
        if (
            example.source_url not in allowed
            or language not in LANGUAGES
            or not example.code.strip()
        ):
            continue
        code, cut = _clip(example.code, limits.max_code_chars)
        truncated = truncated or cut
        examples.append(CodeExample(language, code, example.source_url, cut))

    for note in raw.warnings[:_MAX_WARNINGS]:
        if isinstance(note, str) and note.strip():
            warnings.append(_clip(note, _NOTE_CHARS)[0])
    unresolved = tuple(
        _clip(u, _NOTE_CHARS)[0]
        for u in raw.unresolved[:_MAX_UNRESOLVED]
        if isinstance(u, str) and u.strip()
    )
    return WebSearchReport(
        findings=tuple(findings),
        version_facts=tuple(facts),
        code_examples=tuple(examples),
        sources=sources,
        unresolved=unresolved,
        warnings=tuple(dict.fromkeys(warnings))[: _MAX_WARNINGS + 2],
        rounds_used=rounds_used,
        total_tokens=total_tokens,
        elapsed_sec=round(elapsed_sec, 2),
        truncated=truncated,
    )


@dataclass(frozen=True, slots=True)
class WebSearchRequest:
    """一次联网搜索：要查什么（``query``）和/或要读哪些网页（``urls``）。

    ``topic`` 只是给规划器与事件用的一句话标签；``questions`` 是要回答的问题，
    没给时由工具补一条默认的。
    只给 ``urls`` 时不搜索，只读这些页面；给了 ``query``（或两者都没给）时才检索。
    """

    topic: str
    questions: tuple[str, ...]
    max_sources: int | None = None
    task_id: str = "websearch"
    query: str = ""
    urls: tuple[str, ...] = ()

    @property
    def search_enabled(self) -> bool:
        return bool(self.query) or not self.urls


@dataclass(frozen=True, slots=True)
class Evidence:
    """一个抓取到的来源。``text`` 是原始网页文本，只在 ``WebSearchRunner.run`` 一次调用内存活。"""

    url: str
    title: str
    text: str
    status: str
    artifact_id: str = ""
