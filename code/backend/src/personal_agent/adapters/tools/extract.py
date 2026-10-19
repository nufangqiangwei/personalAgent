"""Pure content extraction (no model, no script execution).

HTML is parsed with the standard library parser; script/style content is dropped
and only text plus heading structure is returned. The output is untrusted data:
it is stored as evidence, never interpreted as instructions.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

MAX_TEXT_CHARS = 200_000

_BLOCK_TAGS = {"p", "div", "li", "br", "tr", "section", "article", "header", "footer"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.headings: list[str] = []
        self._skip_depth = 0
        self._heading_tag: str | None = None
        self._heading_buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
            return
        if tag in _HEADING_TAGS:
            self._heading_tag = tag
            self._heading_buffer = []
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in _HEADING_TAGS and self._heading_tag == tag:
            heading = "".join(self._heading_buffer).strip()
            if heading:
                self.headings.append(heading)
            self._heading_tag = None
            self._heading_buffer = []
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._heading_tag is not None:
            self._heading_buffer.append(data)
        self.parts.append(data)


def extract_text(html: str) -> dict[str, Any]:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    lines = [line.strip() for line in text.splitlines()]
    compact = "\n".join(line for line in lines if line)
    truncated = len(compact) > MAX_TEXT_CHARS
    return {
        "text": compact[:MAX_TEXT_CHARS],
        "headings": parser.headings,
        "truncated": truncated,
        "char_count": min(len(compact), MAX_TEXT_CHARS),
    }


class ContentExtractor:
    """Pure parser used by the ``content.extract`` tool."""

    async def extract(self, *, html: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        result = extract_text(html)
        if schema and schema.get("type") == "object":
            # The tool does not run a model; it returns the parsed structure and
            # lets the engine validate against the declared schema.
            result["schema"] = schema
        return result
