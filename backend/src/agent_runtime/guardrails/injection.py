"""注入样态扫描。设计文档：docs/05_security/03_trust_and_injection.md §5。

纯函数，零 I/O、零模型调用。定位是纵深防御与审计：给可疑内容打标，不拦截、不改写；
误报的代价只是多一行告警，所以宁可宽扫。正则刻意保持简单、量词有界，避免灾难性回溯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

MAX_HITS: Final = 12
_SNIPPET: Final = 80
_I: Final = re.IGNORECASE


@dataclass(frozen=True, slots=True)
class InjectionHit:
    label: str
    snippet: str


_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (label, re.compile(pattern, _I))
    for label, pattern in (
        (
            "instruction_override",
            r"ignore\s{1,5}(?:(?:all|the|any)\s{1,5}){0,2}(?:previous|prior|above|earlier)\s{1,5}"
            r"(?:instructions?|prompts?|rules)",
        ),
        (
            "instruction_override_zh",
            r"忽略(?:之前|以上|上面|先前|前面)的?(?:所有|全部)?(?:指令|指示|提示|规则)",
        ),
        (
            "system_override",
            r"system\s{1,5}override|new\s{1,5}instructions?\s{0,3}[:：]|新指令\s{0,3}[:：]",
        ),
        ("role_switch", r"you\s{1,5}are\s{1,5}now\s{1,5}(?:a|an|the)\b|从现在起你是"),
        ("secrecy", r"(?:do\s{0,3}n['o]?t|never)\s{1,5}tell\s{1,5}the\s{1,5}user|不要告诉用户"),
        (
            "prompt_exfiltration",
            r"reveal\s{1,5}(?:your|the)\s{1,5}system\s{1,5}prompt|输出你的系统提示",
        ),
        ("command_injection", r"run\s{1,5}the\s{1,5}following\s{1,5}command|执行以下命令"),
        (
            "destructive_intent",
            r"rm\s{1,5}-rf\s{1,5}/|\bmkfs\b|dd\s[^\n]{0,40}of=/dev/|:\(\)\s{0,3}\{\s{0,3}:\|:&",
        ),
        (
            "credential_access",
            r"(?:read|cat|send|upload|exfiltrate|print)\b[^\n]{0,40}"
            r"(?:\.env\b|id_rsa|api[_ -]?key|secret[_ -]?key)"
            r"|(?:读取|发送|上传)[^\n]{0,20}(?:\.env|id_rsa|密钥)",
        ),
        (
            "authority_claim",
            r"as\s{1,5}the\s{1,5}(?:administrator|admin|system)\s{0,3},\s{0,3}you\s{1,5}must"
            r"|作为(?:系统|管理员)[，,]\s{0,3}必须",
        ),
        ("hidden_markup", r"<!--[^\n>]{0,200}?(?:ignore|system|instruction)[^\n>]{0,200}?-->"),
    )
)


def scan_injection(text: str | None) -> tuple[InjectionHit, ...]:
    """按文本顺序返回命中，最多 12 处。永不抛异常，出错当作无命中。"""
    if not text:
        return ()
    try:
        found = [
            (match.start(), InjectionHit(label, match.group(0)[:_SNIPPET]))
            for label, pattern in _PATTERNS
            for match in pattern.finditer(text)
        ]
    except (re.error, RecursionError, TypeError):  # pragma: no cover - 防御
        return ()
    found.sort(key=lambda pair: pair[0])
    return tuple(hit for _, hit in found[:MAX_HITS])
