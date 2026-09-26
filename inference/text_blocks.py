"""
Markdown and plain text as document blocks — the shape `office/document.py`
renders to .docx and `office/pdf.py` renders to PDF.

Used by export (a Markdown page saved as PDF or Word) and by import (text
recovered from an uploaded file becoming an editable document). Deliberately
*not* run through `document.validate`: its caps exist to bound what a model
may emit, and a person exporting their own 40-page notes must not be refused
for having written more than a model would. What this produces has the
validated shape — the renderers read nothing else — and long pieces are split
rather than cut, so nothing is lost.

A small reader, not a Markdown implementation: headings, paragraphs, bullet /
numbered / task lists, quotes, fenced code (kept as plain paragraphs), and
pipe tables. Inline `**bold**` and `*italic*` pass through, since both
renderers already understand them.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_BULLET = re.compile(r'^\s*[-*+]\s+(.*)$')
_NUMBERED = re.compile(r'^\s*\d+[.)]\s+(.*)$')
_TASK = re.compile(r'^\[( |x|X)\]\s+(.*)$')
_TABLE_SEP = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')

#: Longest paragraph handed to a renderer in one piece; longer text is split
#: at sentence boundaries so reportlab never has to lay out one giant flowable.
PARAGRAPH_SPLIT = 4000
LIST_SPLIT = 50
TABLE_SPLIT = 200


def _italic_underscores(text: str) -> str:
    # `_word_` is Markdown italic; the renderers read `*word*`.
    return re.sub(r'(?<![\w*])_([^_\n]+?)_(?![\w*])', r'*\1*', text)


def _clean(text: str) -> str:
    text = _italic_underscores(text.strip())
    text = re.sub(r'`([^`]+)`', r'\1', text)                      # inline code
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)           # images → alt
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)       # links → text (url)
    return text


def _split_long(text: str) -> list[str]:
    if len(text) <= PARAGRAPH_SPLIT:
        return [text]
    out, buf = [], ''
    for sentence in re.split(r'(?<=[.!?])\s+', text):
        while len(sentence) > PARAGRAPH_SPLIT:
            out.append(sentence[:PARAGRAPH_SPLIT])
            sentence = sentence[PARAGRAPH_SPLIT:]
        if len(buf) + len(sentence) + 1 > PARAGRAPH_SPLIT and buf:
            out.append(buf)
            buf = ''
        buf = f'{buf} {sentence}'.strip()
    if buf:
        out.append(buf)
    return out


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith('|'):
        line = line[1:]
    if line.endswith('|'):
        line = line[:-1]
    return [_clean(c) for c in line.split('|')]


def markdown_blocks(source: str) -> tuple[str | None, list[dict]]:
    """(first level-1 heading, blocks). The heading is offered as the title."""
    lines = (source or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    blocks: list[dict] = []
    title: str | None = None
    para: list[str] = []
    i = 0

    def flush_para():
        if para:
            text = _clean(' '.join(p.strip() for p in para))
            blocks.extend({'type': 'paragraph', 'text': t} for t in _split_long(text) if t)
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_para()
            i += 1
            continue

        if stripped.startswith('```') or stripped.startswith('~~~'):
            flush_para()
            fence = stripped[:3]
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1
            body = '\n'.join(code).strip('\n')
            if body:
                blocks.extend({'type': 'paragraph', 'text': t} for t in _split_long(body))
            continue

        m = _HEADING.match(stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            text = _clean(m.group(2))
            if level == 1 and title is None and not blocks:
                title = text
            elif text:
                blocks.append({'type': 'heading', 'level': min(level, 3), 'text': text})
            i += 1
            continue

        if (stripped.startswith('|') and i + 1 < len(lines)
                and _TABLE_SEP.match(lines[i + 1])):
            flush_para()
            columns = _cells(stripped)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = _cells(lines[i])
                cells = (cells + [''] * len(columns))[:len(columns)]
                rows.append(cells)
                i += 1
            for n in range(0, max(len(rows), 1), TABLE_SPLIT):
                blocks.append({'type': 'table', 'columns': columns,
                               'rows': rows[n:n + TABLE_SPLIT] or [[''] * len(columns)],
                               'caption': ''})
            continue

        if stripped.startswith('>'):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            text = _clean(' '.join(q for q in quote if q))
            if text:
                blocks.extend({'type': 'quote', 'text': t} for t in _split_long(text))
            continue

        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet or numbered:
            flush_para()
            kind = 'bullets' if bullet else 'numbered'
            pattern = _BULLET if bullet else _NUMBERED
            entries = []
            while i < len(lines):
                m2 = pattern.match(lines[i])
                if not m2:
                    break
                text = m2.group(1).strip()
                task = _TASK.match(text)
                if task:
                    text = ('☑ ' if task.group(1).lower() == 'x' else '☐ ') + task.group(2)
                entries.append(_clean(text)[:1000])
                i += 1
            entries = [e for e in entries if e]
            for n in range(0, len(entries), LIST_SPLIT):
                blocks.append({'type': kind, 'items': entries[n:n + LIST_SPLIT]})
            continue

        para.append(line)
        i += 1

    flush_para()
    return title, blocks


def text_blocks(source: str) -> list[dict]:
    """Plain text: one paragraph per blank-line-separated chunk."""
    blocks = []
    for chunk in re.split(r'\n\s*\n', (source or '').replace('\r\n', '\n')):
        chunk = chunk.strip()
        if chunk:
            blocks.extend({'type': 'paragraph', 'text': t}
                          for t in _split_long(' '.join(chunk.split('\n'))))
    return blocks


def spec_from(title: str, blocks: list[dict], subtitle: str = '') -> dict:
    """A renderable document spec. Never empty: renderers need one block."""
    return {
        'title': (title or 'Untitled')[:200],
        'subtitle': subtitle[:200],
        'theme': 'clean',
        'blocks': blocks or [{'type': 'paragraph', 'text': ' '}],
    }
