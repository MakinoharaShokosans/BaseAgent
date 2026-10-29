"""项目规则槽与提示词库。设计文档：docs/03_context/03_prompt_assembly.md §3.1、§4。

这里有本包唯一的文件 I/O（装配本身是纯函数）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from agent_runtime.context.prompt import ProjectRules

RulesStatus = Literal["absent", "disabled", "enabled"]
_NAME: Final = re.compile(r"[A-Za-z0-9_]+")
_BYTES_PER_CHAR: Final = 4


@dataclass(frozen=True, slots=True)
class RulesResult:
    status: RulesStatus
    rules: ProjectRules | None = None
    #: 检测到的规则文件（相对工作区），未启用时也给出，供界面提示“检测到规则文件，未启用”
    file: str | None = None
    note: str = ""


class RulesCache:
    """按文件的修改时间与大小失效：文件一变，下一次加载重读（该会话的前缀缓存失效一次，是预期的）。"""

    def __init__(self) -> None:
        self.reads = 0
        self._cache: dict[tuple[str, int, int, int], ProjectRules] = {}

    def load(
        self, root: Path, *, allow: bool, candidates: Sequence[str], max_chars: int
    ) -> RulesResult:
        found = next((n for n in candidates if (root / n).is_file()), None)
        if found is None:
            return RulesResult("absent")
        if not allow:
            return RulesResult("disabled", file=found)  # 未启用：连内容都不读
        path = root / found
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(root.resolve()):
                return RulesResult("absent", file=found, note="规则文件位于工作区之外，已忽略")
            info = resolved.stat()
            key = (str(resolved), info.st_mtime_ns, info.st_size, max_chars)
            rules = self._cache.get(key)
            if rules is None:
                rules = self._read(resolved, found, max_chars)
                self._cache = {key: rules}
        except OSError as exc:
            return RulesResult("absent", file=found, note=f"规则文件读取失败：{exc}")
        return RulesResult("enabled", rules, found)

    def _read(self, path: Path, name: str, max_chars: int) -> ProjectRules:
        self.reads += 1
        limit = max_chars * _BYTES_PER_CHAR + _BYTES_PER_CHAR
        with path.open("rb") as handle:
            raw = handle.read(limit)
            more = bool(handle.read(1))
        text = raw.decode("utf-8", errors="replace")
        return ProjectRules(
            text=text[:max_chars], file=name, truncated=more or len(text) > max_chars
        )


class PromptLibrary:
    """按名读取 ``prompts/*.md`` 并缓存；用户覆盖优先，改文件需要显式 ``reload``。"""

    def __init__(
        self, root: Path | None = None, overrides: Mapping[str, str] | None = None
    ) -> None:
        self._root = root or Path(__file__).resolve().parents[1] / "prompts"
        self._cache: dict[str, str] = {}
        self._overrides: dict[str, str] = dict(overrides or {})

    def names(self) -> tuple[str, ...]:
        """随发行版的模板名（不含动态分区与固定指令）。"""
        try:
            names = [path.stem for path in self._root.glob("*.md") if _NAME.fullmatch(path.stem)]
        except OSError:
            return ()
        return tuple(sorted(names))

    def default(self, name: str) -> str:
        """随发行版的初始版本，忽略覆盖。"""
        if not _NAME.fullmatch(name):
            raise KeyError(name)
        if name not in self._cache:
            try:
                self._cache[name] = (self._root / f"{name}.md").read_text(encoding="utf-8")
            except OSError as exc:
                raise KeyError(name) from exc
        return self._cache[name]

    def get(self, name: str) -> str:
        override = self._overrides.get(name)
        if override is not None:
            return override
        return self.default(name)

    def customized(self, name: str) -> bool:
        return name in self._overrides

    def set_override(self, name: str, content: str | None) -> None:
        """``None`` 或空白删除覆盖，回到文件里的初始版本。"""
        if content is None or not content.strip():
            self._overrides.pop(name, None)
        else:
            self._overrides[name] = content

    def overrides(self) -> dict[str, str]:
        return dict(self._overrides)

    def reload(self) -> None:
        self._cache.clear()
