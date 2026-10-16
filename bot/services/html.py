"""Convert the small HTML dialect used by book sites into Discord markdown.

Shared by the Goodreads review embeds and the release blurbs, both of which
receive a fragment of HTML and want a Discord-friendly string.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

_WS_BEFORE_NEWLINE_RE = re.compile(r"[ \t]+\n")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


class _HtmlToMarkdown(HTMLParser):
    """Convert the small HTML dialect used by book sites into Discord markdown."""

    _INLINE = {
        "b": "**",
        "strong": "**",
        "i": "*",
        "em": "*",
        "u": "__",
        "s": "~~",
        "strike": "~~",
        "del": "~~",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._anchors: list[str | None] = []
        self._spans: list[bool] = []
        self._inlines: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        if tag in self._INLINE:
            self.parts.append(self._INLINE[tag])
            self._inlines.append((tag, len(self.parts) - 1))
        elif tag == "br":
            self.parts.append("\n")
        elif tag in ("p", "div"):
            self.parts.append("\n")
        elif tag == "blockquote":
            self.parts.append("\n> ")
        elif tag == "a":
            href = attributes.get("href")
            self._anchors.append(href)
            if href:
                self.parts.append("[")
        elif tag == "img":
            src = attributes.get("src")
            if src:
                self.parts.append(src)
        elif tag == "span":
            spoiler = "spoiler" in (attributes.get("class") or "")
            self._spans.append(spoiler)
            if spoiler:
                self.parts.append("||")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._INLINE:
            self._close_inline(tag)
        elif tag in ("p", "div"):
            self.parts.append("\n")
        elif tag == "blockquote":
            self.parts.append("\n")
        elif tag == "a":
            href = self._anchors.pop() if self._anchors else None
            if href:
                self.parts.append(f"]({href})")
        elif tag == "span":
            if self._spans and self._spans.pop():
                self.parts.append("||")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def _close_inline(self, tag: str) -> None:
        marker = self._INLINE[tag]
        if not (self._inlines and self._inlines[-1][0] == tag):
            self.parts.append(marker)
            return
        _, index = self._inlines.pop()
        inner = "".join(self.parts[index + 1 :])
        if not inner.strip():
            del self.parts[index:]
            return
        # Keep the surrounding whitespace outside the markers; a marker next to
        # whitespace is rendered literally by Discord instead of as emphasis.
        leading = inner[: len(inner) - len(inner.lstrip())]
        trailing = inner[len(inner.rstrip()) :]
        self.parts[index] = leading + self.parts[index]
        self.parts[index + 1 :] = [inner.strip()]
        self.parts.append(marker + trailing)


def html_to_markdown(value: str) -> str:
    """Convert a fragment of HTML into Discord-friendly markdown."""
    parser = _HtmlToMarkdown()
    parser.feed(value or "")
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = _WS_BEFORE_NEWLINE_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
