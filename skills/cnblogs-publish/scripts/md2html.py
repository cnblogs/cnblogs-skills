"""Small, dependency-free Markdown -> HTML converter (GFM-ish subset).

Used only by the ``--format html`` fallback of the cnblogs skill.  The primary
publishing path sends Markdown directly and lets cnblogs render it, so this
converter intentionally stays small and predictable.

Supported: ATX headings, paragraphs, emphasis/strong/strikethrough, inline
code, fenced + indented code, blockquotes, ordered/unordered nested lists,
task list checkboxes, pipe tables, links, images, autolinks, horizontal rules,
raw block HTML passthrough.
"""

from __future__ import annotations

import html
import re

__all__ = ["markdown_to_html"]


def _h(text: str) -> str:
    """Escape text content for HTML."""
    return html.escape(text, quote=False)


def _a(text: str) -> str:
    """Escape an attribute value for HTML."""
    return html.escape(text, quote=True)


_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)\s*$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HTML_BLOCK_RE = re.compile(r"^ {0,3}<([A-Za-z][A-Za-z0-9-]*)(?:\s|>|/)")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")


def markdown_to_html(text: str) -> str:
    """Convert Markdown ``text`` to an HTML fragment."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    body = _blocks(text.split("\n"))
    return "\n".join(part for part in body if part is not None) + "\n"


# --------------------------------------------------------------------------
# inline
# --------------------------------------------------------------------------

def _inline(text: str) -> str:
    stash: list[str] = []

    def keep(value: str) -> str:
        stash.append(value)
        return "\x00%d\x00" % (len(stash) - 1)

    # 1. code spans (highest precedence, content is literal)
    def code_span(m: re.Match) -> str:
        return keep("<code>%s</code>" % _h(m.group(1)))

    text = re.sub(r"`([^`\n]+)`", code_span, text)

    # 2. autolinks and raw inline tags are kept before escaping
    def autolink(m: re.Match) -> str:
        url = m.group(1)
        return keep('<a href="%s">%s</a>' % (_a(url), _h(url)))

    text = re.sub(r"<((?:https?|mailto):[^>\s]+)>", autolink, text)
    text = re.sub(r"<[A-Za-z/!][^>\n]*>", lambda m: keep(m.group(0)), text)

    # 3. escape the remaining text
    text = _h(text)

    # 4. backslash escapes
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!>~|])", lambda m: keep(_h(m.group(1))), text)

    # 5. images (before links)
    def image(m: re.Match) -> str:
        alt = m.group(1)
        url = m.group(2)
        title = m.group(4)
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        title_attr = ' title="%s"' % _a(title) if title else ""
        return keep('<img src="%s" alt="%s"%s />' % (_a(url), _a(alt), title_attr))

    text = re.sub(
        r"!\[([^\]]*)\]\(\s*(<[^>]*>|[^)\s]+)(\s+[\"'](.*?)[\"'])?\s*\)",
        image,
        text,
    )

    # 6. links
    def link(m: re.Match) -> str:
        label = m.group(1)
        url = m.group(2)
        title = m.group(4)
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        title_attr = ' title="%s"' % _a(title) if title else ""
        return keep('<a href="%s"%s>%s</a>' % (_a(url), title_attr, label))

    text = re.sub(
        r"\[([^\]]+)\]\(\s*(<[^>]*>|[^)\s]+)(\s+[\"'](.*?)[\"'])?\s*\)",
        link,
        text,
    )

    # 7. emphasis / strong / strikethrough
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])", r"<strong>\1</strong>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text)
    text = re.sub(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w])_(?=\S)(.+?)(?<=\S)_(?![\w])", r"<em>\1</em>", text)

    # 8. hard line breaks, then collapse soft breaks
    text = re.sub(r" {2,}\n", "<br />\n", text)
    text = re.sub(r"\\\n", "<br />\n", text)
    text = text.replace("\n", " ")

    # 9. restore stashed HTML
    def restore(m: re.Match) -> str:
        return stash[int(m.group(1))]

    return re.sub(r"\x00(\d+)\x00", restore, text)


# --------------------------------------------------------------------------
# block helpers
# --------------------------------------------------------------------------

def _is_table_start(lines: list[str], i: int) -> bool:
    if i + 1 >= len(lines):
        return False
    head, sep = lines[i], lines[i + 1]
    return "|" in head and "|" in sep and bool(_TABLE_SEP_RE.match(sep))


def _starts_block(lines: list[str], i: int) -> bool:
    line = lines[i]
    if _FENCE_RE.match(line) or _HEADING_RE.match(line) or _HR_RE.match(line):
        return True
    if _QUOTE_RE.match(line):
        return True
    if _LIST_RE.match(line) and len(_LIST_RE.match(line).group(1)) <= 8:  # noqa: SIM102
        return True
    if _HTML_BLOCK_RE.match(line):
        return True
    if _is_table_start(lines, i):
        return True
    return False


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
            current.append(ch)
        elif ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    cells.append("".join(current).strip())
    return cells


def _table(lines: list[str], i: int) -> tuple[str, int]:
    header = _split_row(lines[i])
    sep = _split_row(lines[i + 1])
    aligns: list[str | None] = []
    for cell in sep:
        cell = cell.strip()
        left = cell.startswith(":")
        right = cell.endswith(":")
        aligns.append("center" if left and right else "left" if left else "right" if right else None)

    def cells_html(cells: list[str], tag: str) -> str:
        parts = []
        for idx, cell in enumerate(cells):
            align = aligns[idx] if idx < len(aligns) else None
            style = ' style="text-align:%s"' % align if align else ""
            parts.append("<%s%s>%s</%s>" % (tag, style, _inline(cell), tag))
        return "".join(parts)

    out = ["<table>", "<thead><tr>%s</tr></thead>" % cells_html(header, "th"), "<tbody>"]
    i += 2
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        out.append("<tr>%s</tr>" % cells_html(_split_row(lines[i]), "td"))
        i += 1
    out.extend(["</tbody>", "</table>"])
    return "\n".join(out), i


def _list(lines: list[str], i: int) -> tuple[str, int]:
    first = _LIST_RE.match(lines[i])
    assert first is not None
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    items: list[dict] = []

    while i < len(lines):
        line = lines[i]
        match = _LIST_RE.match(line)
        if not line.strip():
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and (len(lines[j]) - len(lines[j].lstrip())) > base:
                if items:
                    items[-1]["lines"].append("")
                i += 1
                continue
            break
        if match:
            indent = len(match.group(1))
            if indent < base:
                break
            if indent == base:
                items.append({"lines": [match.group(3)]})
                i += 1
                continue
            if not items:
                break
            items[-1]["lines"].append(line[base:])
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent > base and items:
            items[-1]["lines"].append(line[base:])
            i += 1
            continue
        break

    tag = "ol" if ordered else "ul"
    rendered = ["<%s>" % tag]
    for item in items:
        content_lines = item["lines"]
        task = _TASK_RE.match(content_lines[0])
        checkbox = ""
        if task:
            checked = " checked" if task.group(1).lower() == "x" else ""
            checkbox = '<input type="checkbox" disabled%s /> ' % checked
            content_lines = [task.group(2)] + content_lines[1:]
        inner = "\n".join(_blocks(content_lines)).strip()
        if inner.startswith("<p>"):
            end = inner.find("</p>")
            if end != -1:
                tail = inner[end + 4:].strip()
                inner = inner[3:end] + (("\n" + tail) if tail else "")
        rendered.append("<li>%s%s</li>" % (checkbox, inner))
    rendered.append("</%s>" % tag)
    return "\n".join(rendered), i


def _blocks(lines: list[str]) -> list[str]:
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            lang = fence.group(2)
            i += 1
            buf: list[str] = []
            closing = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$")
            while i < n and not closing.match(lines[i]):
                buf.append(lines[i])
                i += 1
            if i < n:
                i += 1
            cls = ' class="language-%s"' % _a(lang) if lang else ""
            out.append("<pre><code%s>%s</code></pre>" % (cls, _h("\n".join(buf))))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline(heading.group(2)), level))
            i += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr />")
            i += 1
            continue

        if _QUOTE_RE.match(line):
            buf = []
            while i < n and _QUOTE_RE.match(lines[i]):
                buf.append(re.sub(r"^ {0,3}> ?", "", lines[i]))
                i += 1
            out.append("<blockquote>\n%s\n</blockquote>" % "\n".join(_blocks(buf)))
            continue

        if _is_table_start(lines, i):
            table_html, i = _table(lines, i)
            out.append(table_html)
            continue

        if _LIST_RE.match(line) and len(_LIST_RE.match(line).group(1)) <= 8:
            list_html, i = _list(lines, i)
            out.append(list_html)
            continue

        if line.startswith("    "):
            buf = []
            while i < n and (lines[i].startswith("    ") or not lines[i].strip()):
                buf.append(lines[i][4:] if lines[i].startswith("    ") else "")
                i += 1
            while buf and not buf[-1]:
                buf.pop()
            out.append("<pre><code>%s</code></pre>" % _h("\n".join(buf)))
            continue

        if _HTML_BLOCK_RE.match(line) or line.strip().startswith("<!--"):
            buf = []
            while i < n and lines[i].strip():
                buf.append(lines[i])
                i += 1
            out.append("\n".join(buf))
            continue

        # paragraph
        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not _starts_block(lines, i):
            buf.append(lines[i])
            i += 1
        out.append("<p>%s</p>" % _inline("\n".join(buf)))

    return out
