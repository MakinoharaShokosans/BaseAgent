"""文本文件工具共用的底层：编码、换行、行切分、通配符。

要点是**所有工具对“一行”的理解完全一致**：行以 CRLF、LF、CR 结尾，不含 Unicode 的其他行分隔符
（``str.splitlines`` 把垂直制表符、换页符、U+2028 也当成换行，grep 的行号会与 view_file 对不上）。
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Final, Literal

_LINE_END: Final = re.compile(r"\r\n|\n|\r")
SNIFF_BYTES: Final = 65536
LINE_CLIP: Final = 2000

Eol = Literal["LF", "CRLF", "CR", "mixed", "none"]


@dataclass(frozen=True, slots=True)
class EolStats:
    lf: int
    crlf: int
    cr: int

    @property
    def style(self) -> Eol:
        kinds = [n > 0 for n in (self.lf, self.crlf, self.cr)]
        if sum(kinds) == 0:
            return "none"
        if sum(kinds) > 1:
            return "mixed"
        return "LF" if self.lf else "CRLF" if self.crlf else "CR"

    def describe(self) -> str:
        if self.style == "mixed":
            counts = (("LF", self.lf), ("CRLF", self.crlf), ("CR", self.cr))
            parts = [f"{label} {n} 行" for label, n in counts if n]
            return "换行混合（" + "，".join(parts) + "）"
        return self.style


def eol_stats(text: str) -> EolStats:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    return EolStats(lf, crlf, cr)


def split_lines(text: str) -> list[str]:
    """按 ``\\r\\n`` / ``\\n`` / ``\\r`` 切行；末尾的换行不产生多余的空行。"""
    if not text:
        return []
    parts = _LINE_END.split(text)
    if _LINE_END.search(text[-2:]) and parts and parts[-1] == "":
        parts.pop()
    return parts


@dataclass(frozen=True, slots=True)
class Decoded:
    text: str
    #: ``utf-8`` / ``utf-8-sig``（带 BOM）/ ``utf-16`` / ``gb18030``
    encoding: str
    #: 没能按任何已知编码干净解码，不可解码的字节被替换成了 U+FFFD
    lossy: bool = False

    def label(self) -> str:
        return {
            "utf-8": "",
            "utf-8-sig": "UTF-8 BOM",
            "utf-16": "UTF-16",
            "gb18030": "GB18030（非 UTF-8，已按它解码）",
        }[self.encoding] or ("含无法解码的字节" if self.lossy else "")


def sniff_encoding(sample: bytes) -> str:
    """看开头一段：BOM 定编码；能干净按 UTF-8 解码就是 UTF-8；否则试 GB18030；都不行仍按 UTF-8。"""
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for name in ("utf-8", "gb18030"):
        try:
            # 末尾被截断的多字节字符不算错，所以用增量解码器且 final=False
            codecs.getincrementaldecoder(name)().decode(sample, final=False)
        except UnicodeDecodeError:
            continue
        return name
    return "utf-8"


def decode(data: bytes) -> Decoded:
    encoding = sniff_encoding(data[:SNIFF_BYTES])
    try:
        return Decoded(data.decode(encoding), encoding)
    except UnicodeDecodeError:
        return Decoded(data.decode("utf-8", errors="replace"), "utf-8", lossy=True)


def clip_line(line: str, cap: int = LINE_CLIP) -> str:
    return line if len(line) <= cap else f"{line[:cap]}…[该行共 {len(line)} 字符，已截断]"


def show_whitespace(line: str, terminator: str = "") -> str:
    """把看不见的东西显形：制表符 →，行尾空格 ·，行尾换行 ⏎（CRLF 为 ␍⏎，裸 CR 为 ␍）。"""
    stripped = line.rstrip(" ")
    shown = stripped.replace("\t", "→") + "·" * (len(line) - len(stripped))
    marks = {"\r\n": "␍⏎", "\n": "⏎", "\r": "␍"}
    return shown + marks.get(terminator, "")


# ------------------------------------------------------------------ 通配符


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把 ``**/*.py``、``src/*.{ts,tsx}`` 这类通配符翻成正则，匹配相对于搜索根的 POSIX 路径。

    不含 ``/`` 的通配符只匹配文件名（``*.py`` 命中任意目录下的 py 文件）；
    含 ``/`` 的匹配整条相对路径。
    ``*`` 不跨目录，``**`` 跨目录，``?`` 单个字符，``{a,b}`` 二选一。
    """
    anchored = "/" in pattern
    out: list[str] = []
    i, depth = 0, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 2] == "**":
                i += 2
                if pattern[i : i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "{":
            depth += 1
            out.append("(?:")
        elif ch == "}" and depth:
            depth -= 1
            out.append(")")
        elif ch == "," and depth:
            out.append("|")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append(")" * depth)
    body = "".join(out)
    return re.compile(body if anchored else rf"(?:.*/)?{body}", re.DOTALL)
