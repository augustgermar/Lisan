from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(slots=True)
class Chunk:
    title: str
    body: str
    breadcrumb: str
    source_ref: str
    chunk_index: int
    total_chunks: int


@dataclass(slots=True)
class _Section:
    title: str
    body: str
    breadcrumb: str
    source_ref_base: str
    pages: frozenset[int]
    level: int


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+([A-Z].*?)\s*$")
_ALL_CAPS_HEADING_RE = re.compile(r"^[A-Z0-9][A-Z0-9 ,:/&()'\".-]{4,}$")
_PAGE_MARKER_RE = re.compile(r"^\s*---\s*Page\s+(\d+)\s*---\s*$", re.IGNORECASE)
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n")


def chunk_document(
    text: str,
    title: str,
    mode: str = "auto",
    *,
    source_ref_base: str | None = None,
    min_words: int = 200,
    max_words: int = 1500,
    window_words: int = 800,
    overlap_words: int = 100,
) -> list[Chunk]:
    text = (text or "").strip()
    title = (title or "document").strip() or "document"
    source_ref_base = (source_ref_base or title).strip() or title
    mode = (mode or "auto").strip().lower()
    if not text:
        return []

    if mode == "auto":
        mode = "header" if _has_heading_structure(text) else "sliding"
    if mode == "sliding":
        return _chunk_sliding_window(
            text,
            title=title,
            source_ref_base=source_ref_base,
            window_words=window_words,
            overlap_words=overlap_words,
        )

    sections = _parse_sections(text, title=title, source_ref_base=source_ref_base)
    if not sections:
        return _chunk_sliding_window(
            text,
            title=title,
            source_ref_base=source_ref_base,
            window_words=window_words,
            overlap_words=overlap_words,
        )

    merged = _merge_sections(sections, min_words=min_words)
    chunks: list[Chunk] = []
    for section in merged:
        chunks.extend(_split_section(section, max_words=max_words))
    return _finalize_chunks(chunks)


def _has_heading_structure(text: str) -> bool:
    for line in text.splitlines():
        if _line_is_heading(line):
            return True
    return False


def _parse_sections(text: str, *, title: str, source_ref_base: str) -> list[_Section]:
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = [(0, title)]
    current_title = title
    current_lines: list[str] = []
    current_pages: set[int] = set()
    current_level = 0
    saw_heading = False

    def flush() -> None:
        nonlocal current_lines, current_pages, current_title
        body = "\n".join(_strip_marker_lines(current_lines)).strip()
        if not body:
            current_lines = []
            current_pages = set()
            return
        breadcrumb = " > ".join(part for _, part in stack if part)
        source_ref = _source_ref_for(source_ref_base, breadcrumb, current_pages)
        sections.append(
            _Section(
                title=current_title,
                body=body,
                breadcrumb=breadcrumb,
                source_ref_base=source_ref_base,
                pages=frozenset(current_pages),
                level=current_level,
            )
        )
        current_lines = []
        current_pages = set()

    for line in text.splitlines():
        page_marker = _PAGE_MARKER_RE.match(line)
        if page_marker:
            current_pages.add(int(page_marker.group(1)))
            continue
        heading = _line_is_heading(line)
        if heading:
            flush()
            saw_heading = True
            level, heading_title = heading
            if level == 1 and heading_title.strip().lower() == title.strip().lower():
                current_title = heading_title
                current_level = level
                continue
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading_title))
            current_title = heading_title
            current_level = level
            continue
        current_lines.append(line)

    flush()

    if not saw_heading:
        body = text.strip()
        if body:
            breadcrumb = title
            sections = [
                _Section(
                    title=title,
                    body=body,
                    breadcrumb=breadcrumb,
                    source_ref_base=source_ref_base,
                    pages=frozenset(),
                    level=0,
                )
            ]
    return sections


def _line_is_heading(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    if not stripped:
        return None
    md = _MD_HEADING_RE.match(stripped)
    if md:
        return len(md.group(1)), md.group(2).strip()
    numbered = _NUMBERED_HEADING_RE.match(stripped)
    if numbered and len(stripped.split()) <= 14 and not stripped.endswith((".", ":", ";")):
        return min(6, numbered.group(1).count(".") + 1), stripped
    if _ALL_CAPS_HEADING_RE.match(stripped) and len(stripped.split()) <= 12:
        return 2, stripped.title()
    return None


def _strip_marker_lines(lines: Iterable[str]) -> list[str]:
    return [line for line in lines if not _PAGE_MARKER_RE.match(line)]


def _merge_sections(sections: list[_Section], *, min_words: int) -> list[_Section]:
    merged: list[_Section] = []
    buffer: list[_Section] = []
    for index, section in enumerate(sections):
        last = index == len(sections) - 1
        word_count = _word_count(section.body)
        if word_count < min_words and not last:
            buffer.append(section)
            continue
        if buffer:
            section = _prepend_buffer(section, buffer)
            buffer = []
        merged.append(section)
    if buffer:
        if merged:
            merged[-1] = _prepend_buffer(merged[-1], buffer)
        else:
            merged.extend(buffer)
    return merged


def _prepend_buffer(section: _Section, buffer: list[_Section]) -> _Section:
    prefix_lines: list[str] = []
    pages = set(section.pages)
    for item in buffer:
        pages.update(item.pages)
        prefix_lines.append(f"## {item.title}")
        prefix_lines.append(item.body)
        prefix_lines.append("")
    body = "\n".join(prefix_lines + [section.body]).strip()
    breadcrumb = section.breadcrumb
    return _Section(
        title=section.title,
        body=body,
        breadcrumb=breadcrumb,
        source_ref_base=section.source_ref_base,
        pages=frozenset(pages),
        level=section.level,
    )


def _split_section(section: _Section, *, max_words: int) -> list[Chunk]:
    if _word_count(section.body) <= max_words:
        return [Chunk(
            title=section.title,
            body=section.body,
            breadcrumb=section.breadcrumb,
            source_ref=_source_ref_for(section.source_ref_base, section.breadcrumb, section.pages),
            chunk_index=0,
            total_chunks=0,
        )]

    paragraphs = [para.strip() for para in _PARAGRAPH_SPLIT_RE.split(section.body) if para.strip()]
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    current_words = 0
    for paragraph in paragraphs:
        words = _word_count(paragraph)
        if current and current_words + words > max_words:
            chunks.append(("\n\n".join(current).strip(), ""))
            current = []
            current_words = 0
        current.append(paragraph)
        current_words += words
    if current:
        chunks.append(("\n\n".join(current).strip(), ""))

    total = len(chunks)
    out: list[Chunk] = []
    for index, (body, _) in enumerate(chunks):
        part_title = f"{section.title} (part {index + 1} of {total})"
        part_breadcrumb = f"{section.breadcrumb} > part {index + 1} of {total}"
        out.append(
            Chunk(
                title=part_title,
                body=body,
                breadcrumb=part_breadcrumb,
                source_ref=_source_ref_for(section.source_ref_base, section.breadcrumb, section.pages, part=index + 1, total=total),
                chunk_index=index,
                total_chunks=total,
            )
        )
    return out


def _chunk_sliding_window(
    text: str,
    *,
    title: str,
    source_ref_base: str,
    window_words: int,
    overlap_words: int,
) -> list[Chunk]:
    words = "\n".join(_strip_marker_lines(text.splitlines())).split()
    if not words:
        return []
    if len(words) <= window_words:
        return [Chunk(
            title=title,
            body=text.strip(),
            breadcrumb=title,
            source_ref=source_ref_base,
            chunk_index=0,
            total_chunks=1,
        )]

    chunks: list[Chunk] = []
    start = 0
    index = 0
    while start < len(words):
        end = min(len(words), start + window_words)
        body = " ".join(words[start:end]).strip()
        chunks.append(
            Chunk(
                title=f"{title} — segment {index + 1}",
                body=body,
                breadcrumb=f"{title} > segment {index + 1}",
                source_ref=f"{source_ref_base}, segment {index + 1}",
                chunk_index=index,
                total_chunks=0,
            )
        )
        if end >= len(words):
            break
        start = max(end - overlap_words, start + 1)
        index += 1
    return _finalize_chunks(chunks)


def _finalize_chunks(chunks: list[Chunk]) -> list[Chunk]:
    total = len(chunks)
    finalized: list[Chunk] = []
    for index, chunk in enumerate(chunks):
        finalized.append(
            Chunk(
                title=chunk.title,
                body=chunk.body,
                breadcrumb=chunk.breadcrumb,
                source_ref=chunk.source_ref,
                chunk_index=index,
                total_chunks=total,
            )
        )
    return finalized


def _word_count(text: str) -> int:
    return len([part for part in text.split() if part.strip()])


def _source_ref_for(
    source_ref_base: str,
    breadcrumb: str,
    pages: frozenset[int],
    *,
    part: int | None = None,
    total: int | None = None,
) -> str:
    parts = [source_ref_base]
    if breadcrumb:
        parts.append(breadcrumb)
    if pages:
        low = min(pages)
        high = max(pages)
        parts.append(f"pages {low}-{high}" if low != high else f"page {low}")
    if part and total:
        parts.append(f"part {part} of {total}")
    return ", ".join(parts)
