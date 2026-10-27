"""正文清洗：HTML 到保留代码块的 Markdown 风格文本。

有 ``trafilatura`` 就用它；没有就用标准库的回退实现（去脚本与样式，保留标题、段落、列表、代码块）。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Final

_SKIP: Final = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe", "head", "nav", "footer"}
)
_BLOCK: Final = frozenset(
    {"p", "div", "section", "article", "li", "tr", "br", "ul", "ol", "table", "blockquote"}
)
_HEADINGS: Final = {
    "h1": "# ",
    "h2": "## ",
    "h3": "### ",
    "h4": "#### ",
    "h5": "##### ",
    "h6": "###### ",
}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag in _HEADINGS:
            self.parts.append("\n\n" + _HEADINGS[tag])
        elif tag == "pre":
            self._pre += 1
            self.parts.append("\n\n```\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(self._skip - 1, 0)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(self._pre - 1, 0)
            self.parts.append("\n```\n\n")
        elif tag in _HEADINGS or tag in {"p", "div", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data if self._pre else re.sub(r"\s+", " ", data))


def extract_text(html: str) -> str:
    try:
        import trafilatura  # type: ignore[import-not-found]

        text = trafilatura.extract(html, output_format="markdown", include_formatting=True)
        if text:
            return str(text).strip()
    except ImportError:
        pass
    parser = _Text()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def title_of(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", match.group(1)).strip()[:200] if match else ""
