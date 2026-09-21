from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
from typing import Iterable

# Chunks are embedded as ``summary + "\n\n" + body`` (summary is the chunk
# breadcrumb) by BAAI/bge-small-en-v1.5, which reads at most 512 tokens and
# silently drops the rest. Keep every chunk body inside that window, with
# headroom for the breadcrumb summary and the [CLS]/[SEP] special tokens.
EMBEDDING_MAX_TOKENS = 512
EMBEDDING_RESERVE_TOKENS = 64
DEFAULT_MAX_TOKENS = EMBEDDING_MAX_TOKENS - EMBEDDING_RESERVE_TOKENS
DEFAULT_OVERLAP_TOKENS = 64


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
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[Chunk]:
    """Split ``text`` into chunks of at most ``max_tokens`` embedding-model
    tokens. The word limits still apply; whichever limit is hit first wins."""
    max_tokens = max(16, int(max_tokens))
    overlap_tokens = min(DEFAULT_OVERLAP_TOKENS, max_tokens // 5)
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
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )

    sections = _parse_sections(text, title=title, source_ref_base=source_ref_base)
    if not sections:
        return _chunk_sliding_window(
            text,
            title=title,
            source_ref_base=source_ref_base,
            window_words=window_words,
            overlap_words=overlap_words,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )

    merged = _merge_sections(sections, min_words=min_words)
    chunks: list[Chunk] = []
    for section in merged:
        chunks.extend(_split_section(
            section, max_words=max_words, max_tokens=max_tokens, overlap_tokens=overlap_tokens,
        ))
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


def _split_section(
    section: _Section, *, max_words: int, max_tokens: int, overlap_tokens: int,
) -> list[Chunk]:
    if _word_count(section.body) <= max_words and _token_count(section.body) <= max_tokens:
        return [Chunk(
            title=section.title,
            body=section.body,
            breadcrumb=section.breadcrumb,
            source_ref=_source_ref_for(section.source_ref_base, section.breadcrumb, section.pages),
            chunk_index=0,
            total_chunks=0,
        )]

    paragraphs = [para.strip() for para in _PARAGRAPH_SPLIT_RE.split(section.body) if para.strip()]
    # HTML extraction often produces one enormous whitespace-separated
    # section. Treat it as a sliding window rather than emitting one
    # unbounded chunk, which would make a large standard look ingested while
    # retrieval could only see its first result-sized fragment.
    if len(paragraphs) == 1 and _oversized(paragraphs[0], max_words, max_tokens):
        return _chunk_sliding_window(
            paragraphs[0], title=section.title, source_ref_base=section.source_ref_base,
            window_words=max_words, overlap_words=min(100, max_words // 5),
            max_tokens=max_tokens, overlap_tokens=overlap_tokens,
        )
    chunks: list[tuple[str, str]] = []
    current: list[str] = []
    current_words = 0
    current_tokens = 0
    for paragraph in paragraphs:
        # A single paragraph can itself exceed the window; window it on its
        # own so no packed chunk is ever oversized.
        if _oversized(paragraph, max_words, max_tokens):
            if current:
                chunks.append(("\n\n".join(current).strip(), ""))
                current, current_words, current_tokens = [], 0, 0
            for window in _window_texts(paragraph, max_words, max_words // 5, max_tokens, overlap_tokens):
                chunks.append((window, ""))
            continue
        words = _word_count(paragraph)
        tokens = _token_count(paragraph)
        if current and (current_words + words > max_words or current_tokens + tokens > max_tokens):
            chunks.append(("\n\n".join(current).strip(), ""))
            current, current_words, current_tokens = [], 0, 0
        current.append(paragraph)
        current_words += words
        current_tokens += tokens
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
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    clean = "\n".join(_strip_marker_lines(text.splitlines()))
    if not clean.split():
        return []
    if not _oversized(clean, window_words, max_tokens):
        return [Chunk(
            title=title,
            body=text.strip(),
            breadcrumb=title,
            source_ref=source_ref_base,
            chunk_index=0,
            total_chunks=1,
        )]

    chunks: list[Chunk] = []
    for index, body in enumerate(_window_texts(clean, window_words, overlap_words, max_tokens, overlap_tokens)):
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
    return _finalize_chunks(chunks)


def _window_texts(
    text: str, window_words: int, overlap_words: int, max_tokens: int, overlap_tokens: int,
) -> list[str]:
    """Greedy sliding windows over whitespace words, closed when either the
    word or the token budget would be exceeded, with overlap on both axes."""
    words = text.split()
    costs = _word_token_costs(words)
    out: list[str] = []
    start = 0
    while start < len(words):
        end = start
        tokens = 0
        while end < len(words) and end - start < window_words and (
            end == start or tokens + costs[end] <= max_tokens
        ):
            tokens += costs[end]
            end += 1
        out.append(" ".join(words[start:end]).strip())
        if end >= len(words):
            break
        back_words, back_tokens = 0, 0
        while (
            back_words < overlap_words
            and back_tokens + costs[end - 1 - back_words] <= overlap_tokens
            and end - 1 - back_words > start
        ):
            back_tokens += costs[end - 1 - back_words]
            back_words += 1
        start = max(end - back_words, start + 1)
    return out


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


def _oversized(text: str, max_words: int, max_tokens: int) -> bool:
    return _word_count(text) > max_words or _token_count(text) > max_tokens


@lru_cache(maxsize=1)
def _tokenizer():
    """The embedding model's own tokenizer, loaded from the local fastembed
    cache (never downloaded). None when unavailable -> conservative estimate."""
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return None
    for root in (Path.home() / ".cache" / "lisan" / "fastembed", Path.home() / ".cache" / "fastembed"):
        for candidate in sorted(root.glob("models--*bge-small-en-v1.5*/snapshots/*/tokenizer.json")):
            try:
                tok = Tokenizer.from_file(str(candidate))
                tok.no_truncation()
                return tok
            except Exception:
                continue
    return None


def _word_token_costs(words: list[str]) -> list[int]:
    """Token cost of each whitespace word. WordPiece pre-tokenizes on
    whitespace, so per-word costs sum to the cost of the joined text."""
    if not words:
        return []
    tok = _tokenizer()
    if tok is None:
        # No tokenizer available: ~3 chars per token is deliberately pessimistic
        # for English so chunks err small rather than getting truncated.
        return [max(1, -(-len(word) // 3)) for word in words]
    return [max(1, len(enc.ids)) for enc in tok.encode_batch(words, add_special_tokens=False)]


def _token_count(text: str) -> int:
    return sum(_word_token_costs(text.split()))


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
