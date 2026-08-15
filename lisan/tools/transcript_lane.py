"""A scoped, in-memory retrieval lane for conversation transcripts.

Transcripts are deliberately excluded from Lisan's global retrieval index:
they are raw, duplicate the distilled memory, and are useful here precisely
because they preserve wording that the writer may have dropped.  Enrichment
passes explicit file pointers to this lane; it never discovers or scans the
user's transcript corpus on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_BLOCK_RE = re.compile(
    r"(?:^|\n)## Conversation — (?P<time>[^\n]+)\n\n(?P<body>.*?)(?=\n## Conversation — |\Z)",
    re.DOTALL,
)
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class TranscriptHit:
    path: str
    excerpt: str
    score: float
    conversation: str = ""


@dataclass(frozen=True, slots=True)
class _TranscriptBlock:
    path: Path
    header: str
    body: str


def _terms(query: str) -> list[str]:
    return list(dict.fromkeys(t.lower() for t in _WORD_RE.findall(query) if len(t) > 1))


def _blocks(path: Path) -> Iterable[_TranscriptBlock]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = list(_BLOCK_RE.finditer(text))
    if not found and text.strip():
        return [_TranscriptBlock(path, "", text.strip())]
    return [_TranscriptBlock(path, m.group("time"), m.group("body").strip()) for m in found]


class TranscriptLane:
    """Ephemeral transcript index built only from caller-supplied paths."""

    def __init__(self) -> None:
        self._blocks: list[_TranscriptBlock] = []

    def add(self, path: Path) -> None:
        self._blocks.extend(_blocks(path))

    def add_many(self, paths: Iterable[Path]) -> None:
        for path in paths:
            self.add(Path(path))

    def search(self, query: str, *, limit: int = 5, preferred: Path | None = None) -> list[TranscriptHit]:
        terms = _terms(query)
        if not terms:
            return []
        hits: list[TranscriptHit] = []
        for block in self._blocks:
            haystack = f"{block.header}\n{block.body}".lower()
            matched = sum(1 for term in terms if term in haystack)
            if not matched:
                continue
            exact_phrase = query.strip().lower() in haystack if query.strip() else False
            preferred_bonus = 1.0 if preferred and block.path == preferred else 0.0
            score = (matched / len(terms)) + (1.0 if exact_phrase else 0.0) + preferred_bonus
            excerpt = _excerpt(block.body, terms)
            hits.append(TranscriptHit(str(block.path), excerpt, score, block.header))
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.conversation))
        return hits[: max(1, int(limit))]


def _excerpt(text: str, terms: list[str], max_chars: int = 1200) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(term in lowered for term in terms):
            return line[:max_chars]
    return " ".join(lines)[:max_chars]


def search_transcripts(
    query: str,
    *,
    current: Path | None = None,
    historical: Iterable[Path] = (),
    limit: int = 5,
) -> list[TranscriptHit]:
    lane = TranscriptLane()
    if current is not None:
        lane.add(current)
    lane.add_many(historical)
    return lane.search(query, limit=limit, preferred=current)
