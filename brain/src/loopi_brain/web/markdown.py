"""A small, safe markdown renderer for recaps: headings, "- " lists, paragraphs and **bold**.

Every character is HTML-escaped before any markup is added, so text written by a model can never inject
HTML, links, images or scripts. Nothing else of markdown is supported (and nothing else is needed).
"""
from __future__ import annotations

import re

from markupsafe import Markup, escape

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"(#{1,4})\s+(.*)")


def _inline(text: str) -> str:
    return _BOLD.sub(r"<strong>\1</strong>", str(escape(text)))


def render_markdown(text: str, heading_offset: int = 1) -> Markup:
    """``text`` as HTML; ``## x`` becomes ``<h3>`` with the default offset (the page already has h1/h2)."""
    html: list[str] = []
    items: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        if items:
            html.append("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
            items.clear()
        if paragraph:
            html.append("<p>" + " ".join(paragraph) + "</p>")
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        heading = _HEADING.fullmatch(line)
        if not line:
            flush()
        elif heading:
            flush()
            level = min(len(heading.group(1)) + heading_offset, 6)
            html.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif line.startswith(("- ", "* ")):
            if paragraph:
                flush()
            items.append(_inline(line[2:].strip()))
        else:
            if items:
                flush()
            paragraph.append(_inline(line))
    flush()
    return Markup("\n".join(html))
