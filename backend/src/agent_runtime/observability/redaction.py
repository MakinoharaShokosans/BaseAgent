"""统一脱敏：事件、轨迹、日志共用，只实现一次。设计文档：docs/08_observability/02 §1.4。

尽力而为，不是保证：键名规则、已知密钥形态、命令里的凭据片段、高熵长字符串、金丝雀。
纯函数集合，不依赖任何业务层。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

REDACTED: Final = "[REDACTED]"

#: 命令里的凭据片段。每条的第 1 组是要保留的前缀，其后的值被替换。
_COMMAND_RULES: Final[tuple[re.Pattern[str], ...]] = (
    # 请求头：Authorization: Bearer xxx / Authorization: xxx
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+|basic\s+|token\s+)?)[^\s'\"]+"),
    # 选项：--password xxx / --token=xxx / --api-key xxx
    re.compile(
        r"(?i)(--?(?:password|passwd|pass|token|secret|api[-_]?key|access[-_]?key|auth)(?:=|\s+))"
        r"[^\s'\"]+"
    ),
    # 环境变量赋值：API_KEY=xxx、GITHUB_TOKEN=xxx
    re.compile(r"(?i)(\b[A-Za-z0-9_]*(?:key|token|secret|password|passwd)[A-Za-z0-9_]*=)[^\s'\"]+"),
    # URL 里的凭据：https://user:pass@host
    re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@'\"]+:)[^\s/@'\"]+(?=@)"),
)

_ENTROPY_TOKEN: Final = re.compile(r"[A-Za-z0-9_\-+]{32,}={0,2}")
_ENTROPY_THRESHOLD: Final = 4.3
_MIN_SECRET_CHARS: Final = 6


def _entropy(text: str) -> float:
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in Counter(text).values())


def _looks_secret(token: str) -> bool:
    classes = (
        any(c.islower() for c in token),
        any(c.isupper() for c in token),
        any(c.isdigit() for c in token),
    )
    return sum(classes) >= 2 and _entropy(token) >= _ENTROPY_THRESHOLD


class Redactor:
    """``enabled=False`` 时原样返回（仅供测试与明确关闭的场景）。"""

    def __init__(
        self,
        key_patterns: Iterable[str],
        value_patterns: Iterable[str],
        *,
        enabled: bool = True,
        secrets: Iterable[str] = (),
    ) -> None:
        self._enabled = enabled
        self._keys = tuple(p.lower() for p in key_patterns if p)
        self._key_sources = tuple(key_patterns)
        self._value_sources = tuple(value_patterns)
        self._values = tuple(re.compile(p, re.IGNORECASE) for p in value_patterns)
        self._secrets = tuple(sorted({s for s in secrets if len(s) >= _MIN_SECRET_CHARS}, key=len))[
            ::-1
        ]

    def with_secrets(self, secrets: Iterable[str]) -> Redactor:
        """追加需要精确替换的字符串（金丝雀）。返回新对象，原对象不变。"""
        return Redactor(
            self._key_sources,
            self._value_sources,
            enabled=self._enabled,
            secrets=(*self._secrets, *secrets),
        )

    # ------------------------------------------------------------------ 文本

    def text(self, value: str) -> str:
        if not self._enabled or not value:
            return value
        out = value
        for secret in self._secrets:
            out = out.replace(secret, REDACTED)
        for pattern in self._values:
            out = pattern.sub(REDACTED, out)
        for rule in _COMMAND_RULES:
            out = rule.sub(lambda m: m.group(1) + REDACTED, out)
        return _ENTROPY_TOKEN.sub(
            lambda m: REDACTED if _looks_secret(m.group(0)) else m.group(0), out
        )

    # ------------------------------------------------------------------ 结构

    def is_secret_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(p in lowered for p in self._keys)

    def value(self, value: object) -> object:
        """递归处理 JSON 样的结构。返回新对象，不改入参。"""
        if not self._enabled:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {
                k: REDACTED if isinstance(k, str) and self.is_secret_key(k) else self.value(v)
                for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
            }
        if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
            return [self.value(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return value

    def args(self, args: Mapping[str, object]) -> tuple[dict[str, object], bool]:
        """工具入参：返回脱敏后的副本与“是否发生过替换”（轨迹的 ``tool_args_redacted``）。"""
        cleaned = self.value(args)
        assert isinstance(cleaned, dict) or not self._enabled
        out: dict[str, object] = dict(cleaned) if isinstance(cleaned, dict) else dict(args)  # pyright: ignore[reportUnknownArgumentType]
        return out, out != dict(args)
