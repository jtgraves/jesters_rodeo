from __future__ import annotations

import re

from markupsafe import Markup, escape

# A small, explicitly-safe formatting subset for admin-authored prose
# (event/charity/beneficiary descriptions, clown bios, FAQ answers) -- not a
# general Markdown implementation. Supports:
#   # Heading / ## Subheading      -> <h3> / <h4>
#   **bold**                       -> <strong>
#   *italic*                       -> <em>
#   - list item (one per line)     -> <ul><li>
#   [label](https://example.com)   -> <a>  (http(s) only, otherwise left as
#                                            plain text -- same rule as the
#                                            Clowns Resources links)
# A blank line starts a new paragraph; a single line break within one
# becomes <br> (this matches the plain-text + `white-space: pre-line`
# behavior these fields had before formatting existed).
#
# Safety comes from ORDER, not from an allow-list applied afterward: the
# entire input is HTML-escaped first, so no markup typed or pasted into the
# field can ever reach the page as a tag -- every tag in the output is one
# this module introduces itself, over text that was already made inert.
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_LIST_ITEM_RE = re.compile(r"^[-*]\s+(.*)$")
_LINK_RE = re.compile(r"\[([^\[\]]+)\]\((https?://[^\s()]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"\*(.+?)\*")

# Headings start at h3: every page that shows one of these fields already
# owns h1 (the page/section title) and h2 (e.g. "About this event", a
# beneficiary's name, a lieutenant's name) immediately above it, so content
# typed inside the field must nest below that, never collide with it.
_HEADING_TAGS = {1: "h3", 2: "h4", 3: "h5"}


def _inline(text: str) -> str:
    """Apply inline formatting to one line of already-escaped text.

    Links before bold before italic: a link label may itself be bold, and
    bold must be resolved before italic so **x** isn't read as *,*x*,*.
    """
    text = _LINK_RE.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        text,
    )
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    return text


def render_richtext(value: str | None) -> Markup:
    """Render the formatting subset described above to safe HTML."""
    if not value:
        return Markup("")

    lines = str(escape(value)).replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            tag = _HEADING_TAGS[len(heading.group(1))]
            parts.append(f"<{tag}>{_inline(heading.group(2).strip())}</{tag}>")
            i += 1
            continue

        if _LIST_ITEM_RE.match(line):
            items = []
            while i < n and _LIST_ITEM_RE.match(lines[i]):
                items.append(f"<li>{_inline(_LIST_ITEM_RE.match(lines[i]).group(1))}</li>")
                i += 1
            parts.append(f"<ul>{''.join(items)}</ul>")
            continue

        para_lines = []
        while i < n and lines[i].strip() and not _HEADING_RE.match(lines[i]) \
                and not _LIST_ITEM_RE.match(lines[i]):
            para_lines.append(lines[i])
            i += 1
        parts.append(f"<p>{'<br>'.join(_inline(l) for l in para_lines)}</p>")

    return Markup("".join(parts))
