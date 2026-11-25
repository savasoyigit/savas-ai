"""HTML -> plain text and link tokens; listing text is never rendered as HTML."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}
SKIP_TAGS = {"script", "style", "head", "title"}


class Tokens(HTMLParser):
    """("text", str) and ("link", href, text) in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[tuple] = []
        self.href: str | None = None
        self.link_text: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "a":
            self.href = dict(attrs).get("href") or ""
            self.link_text = []
        elif tag in BLOCK_TAGS:
            self.out.append(("break",))

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href is not None:
            self.out.append(("link", html.unescape(self.href), squash(" ".join(self.link_text))))
            self.href = None
        elif tag in BLOCK_TAGS:
            self.out.append(("break",))

    def handle_data(self, data):
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
        elif data.strip():
            self.out.append(("text", squash(data)))


def squash(text: str) -> str:
    return " ".join((text or "").split())


def tokens(markup: str) -> list[tuple]:
    parser = Tokens()
    parser.feed(markup or "")
    parser.close()
    return parser.out


def to_text(markup: str, limit: int = 20000) -> str:
    """Readable text with paragraph breaks."""
    lines, current = [], []
    for token in tokens(markup):
        if token[0] == "break":
            if current:
                lines.append(" ".join(current))
                current = []
        elif token[0] == "text":
            current.append(token[1])
        elif token[2]:
            current.append(token[2])
    if current:
        lines.append(" ".join(current))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line.strip()))
    return text[:limit]


def clip(text: str, limit: int) -> str:
    text = squash(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"
