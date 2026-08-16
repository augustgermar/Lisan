"""A private, enrichment-only retrieval lane for conversation transcripts.

Transcripts remain deliberately excluded from Lisan's global retrieval index:
they are raw, duplicate distilled memory, and should not change ordinary chat
retrieval.  This lane discovers transcript files only when enrichment asks it
to close a named deficit.  It keeps a small sidecar index beside the SQLite
index, with stable content and embedding hashes so changed conversations are
re-embedded without rebuilding the ordinary index.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..config import load_config
from ..providers.embeddings import EmbeddingProvider
from .vector_store import cosine_similarity


_BLOCK_RE = re.compile(
    r"(?:^|\n)## Conversation — (?P<time>[^\n]+)\n\n(?P<body>.*?)(?=\n## Conversation — |\Z)",
    re.DOTALL,
)
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)
_INDEX_VERSION = 1


@dataclass(frozen=True, slots=True)
class TranscriptHit:
    path: str
    excerpt: str
    score: float
    conversation: str = ""
    content_hash: str = ""
    embedding_hash: str = ""
    embedding_score: float = 0.0


@dataclass(frozen=True, slots=True)
class _TranscriptBlock:
    path: Path
    header: str
    body: str
    record_id: str
    content_hash: str


def _terms(query: str) -> list[str]:
    return list(dict.fromkeys(t.lower() for t in _WORD_RE.findall(query) if len(t) > 1))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _blocks(path: Path) -> Iterable[_TranscriptBlock]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = list(_BLOCK_RE.finditer(text))
    raw_blocks = [(m.group("time"), m.group("body").strip()) for m in found]
    if not raw_blocks and text.strip():
        raw_blocks = [("", text.strip())]
    resolved = path.resolve()
    for header, body in raw_blocks:
        content_hash = _hash(body)
        record_id = _hash(f"{resolved}\0{header}\0{body}")
        yield _TranscriptBlock(path, header, body, record_id, content_hash)


def _excerpt(text: str, terms: list[str], max_chars: int = 1200) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(term in lowered for term in terms):
            return line[:max_chars]
    return " ".join(lines)[:max_chars]


def _sidecar_path(vault: Path, db_path: Path | None, index_path: Path | None) -> Path:
    if index_path is not None:
        return Path(index_path)
    if db_path is not None:
        return Path(db_path).parent / "transcript_embeddings.bin"
    return vault / "transcript_embeddings.bin"


def _discover(vault: Path, explicit: Iterable[Path]) -> list[Path]:
    paths: dict[str, Path] = {}
    root = vault / "transcripts"
    if root.exists():
        for path in sorted(root.rglob("*.md")):
            paths[str(path.resolve())] = path
    for path in explicit:
        candidate = Path(path)
        if candidate.exists() and candidate.is_file():
            paths[str(candidate.resolve())] = candidate
    return list(paths.values())


def _load_sidecar(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not path.exists():
        return {}, {}
    header: dict[str, Any] = {}
    records: dict[str, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}, {}
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if "__meta__" in payload:
            header = dict(payload["__meta__"] or {})
        elif payload.get("id"):
            records[str(payload["id"])] = payload
    return header, records


def _write_sidecar(path: Path, header: dict[str, Any], records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"__meta__": {"version": _INDEX_VERSION, **header}}, sort_keys=True)]
    lines.extend(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _provider_identity(provider: EmbeddingProvider) -> str:
    settings = getattr(provider, "settings", {}) or {}
    return json.dumps(
        {
            "provider": settings.get("provider"),
            "model": settings.get("model"),
            "mode": settings.get("mode"),
            "dimensions": settings.get("hash_dimensions"),
        }, sort_keys=True, default=str,
    )


class TranscriptLane:
    """Persistent, scoped transcript index with lexical + vector ranking."""

    def __init__(
        self,
        *,
        vault: Path | None = None,
        db_path: Path | None = None,
        index_path: Path | None = None,
        config: dict[str, Any] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.vault = Path(vault).resolve() if vault is not None else None
        self.db_path = Path(db_path) if db_path is not None else None
        self.index_path = _sidecar_path(self.vault or Path.cwd(), self.db_path, index_path)
        self.config = config or load_config()
        self.embedding_provider = embedding_provider or EmbeddingProvider(self.config)
        self._blocks_by_id: dict[str, _TranscriptBlock] = {}

    def add(self, path: Path) -> None:
        for block in _blocks(path):
            self._blocks_by_id[block.record_id] = block

    def add_many(self, paths: Iterable[Path]) -> None:
        for path in paths:
            self.add(Path(path))

    def _ensure_embeddings(self) -> tuple[dict[str, dict[str, Any]], str]:
        header, old = _load_sidecar(self.index_path)
        identity = _provider_identity(self.embedding_provider)
        reusable = old if str(header.get("provider_identity") or "") == identity else {}
        pending = [block for block in self._blocks_by_id.values() if block.record_id not in reusable]
        outcomes: dict[str, tuple[list[float] | None, str, str, int, str]] = {}
        if pending:
            result = self.embedding_provider.embed_records([block.body for block in pending])
            for block, vector in zip(pending, result.vectors):
                mode = result.mode_used if vector is not None else "skip"
                model = result.model
                dimension = result.dimension
                embedding_hash = _hash(f"{model}\0{block.content_hash}") if vector is not None else ""
                outcomes[block.record_id] = (vector, mode, embedding_hash, dimension, model)

        current: dict[str, dict[str, Any]] = {}
        model = str(header.get("model") or "")
        dimension = int(header.get("dimension") or 0)
        for block in self._blocks_by_id.values():
            prior = reusable.get(block.record_id)
            if prior is not None and str(prior.get("content_hash") or "") == block.content_hash:
                current[block.record_id] = prior
                model = str(prior.get("model") or model)
                dimension = int(prior.get("dimension") or dimension)
                continue
            match = outcomes.get(block.record_id)
            if match is None:
                continue
            vector, mode, embedding_hash, item_dimension, item_model = match
            record: dict[str, Any] = {
                "id": block.record_id,
                "path": str(block.path),
                "conversation": block.header,
                "content_hash": block.content_hash,
                "embedding_hash": embedding_hash,
                "mode": mode,
                "model": item_model,
                "dimension": int(item_dimension),
            }
            if vector is not None:
                record["embedding"] = vector
                model = item_model
                dimension = int(item_dimension)
            current[block.record_id] = record

        # A transcript is private to this lane; the sidecar never enters the
        # ordinary files table or ordinary embeddings.bin.
        _write_sidecar(
            self.index_path,
            {"provider_identity": identity, "model": model, "dimension": dimension},
            current.values(),
        )
        return current, identity

    def search(self, query: str, *, limit: int = 5, preferred: Path | None = None) -> list[TranscriptHit]:
        terms = _terms(query)
        if not terms or not self._blocks_by_id:
            return []
        records, _ = self._ensure_embeddings()
        query_result = self.embedding_provider.embed_query(query)
        query_vector = query_result.vector
        semantic_enabled = query_result.mode_used == "semantic"
        hits: list[TranscriptHit] = []
        for record_id, block in self._blocks_by_id.items():
            record = records.get(record_id) or {}
            haystack = f"{block.header}\n{block.body}".lower()
            matched = sum(1 for term in terms if term in haystack)
            lexical = (matched / len(terms)) + (1.0 if query.strip().lower() in haystack else 0.0)
            vector = record.get("embedding")
            semantic = cosine_similarity(query_vector or [], vector or []) if semantic_enabled and record.get("mode") == "semantic" else 0.0
            if matched == 0 and semantic <= 0:
                continue
            if query_result.vector and vector:
                score = (semantic * 0.7) + (min(1.0, lexical) * 0.3)
            else:
                score = lexical
            if preferred is not None and block.path.resolve() == preferred.resolve():
                score += 0.05
            hits.append(TranscriptHit(
                path=str(block.path), excerpt=_excerpt(block.body, terms), score=score,
                conversation=block.header, content_hash=block.content_hash,
                embedding_hash=str(record.get("embedding_hash") or ""), embedding_score=semantic,
            ))
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.conversation))
        return hits[: max(1, int(limit))]


def search_transcripts(
    query: str,
    *,
    vault: Path | None = None,
    db_path: Path | None = None,
    current: Path | None = None,
    historical: Iterable[Path] = (),
    limit: int = 5,
    config: dict[str, Any] | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> list[TranscriptHit]:
    """Search current and automatically discovered historical transcripts.

    Explicit paths remain accepted for disposable tests and special callers;
    normal enrichment supplies the vault and receives the complete local
    transcript corpus through this lane only.
    """
    root = Path(vault).resolve() if vault is not None else None
    lane = TranscriptLane(
        vault=root, db_path=db_path, config=config, embedding_provider=embedding_provider,
    )
    explicit = ([current] if current is not None else []) + list(historical)
    if root is not None:
        explicit = _discover(root, explicit)
    lane.add_many(explicit)
    return lane.search(query, limit=limit, preferred=current)
