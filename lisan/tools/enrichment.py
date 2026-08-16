"""Ship 2 self-enrichment: bounded, transcript-first knowledge seeking."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..frontmatter import load_markdown, write_markdown
from ..providers.embeddings import EmbeddingProvider
from ..utils import today_iso
from .action_policy import action_allowed
from .research import LocalRootProvider, SourceFinding, SourceProvider, search_owner_sources
from .retrieval import retrieve_context
from .transcript_lane import TranscriptHit, search_transcripts


MAX_INFERENCE_CONFIDENCE = 0.6


@dataclass(frozen=True, slots=True)
class EnrichmentResolution:
    text: str
    source_type: str
    source_uri: str
    basis: str = "direct_evidence"
    confidence: float | None = None
    claim_key: str = ""


def seek(
    *,
    vault: Path,
    db_path: Path | None,
    loop_id: str,
    deficit_id: str,
    deficit: str,
    entity_path: Path,
    config: dict[str, Any] | None = None,
    current_transcript: Path | None = None,
    historical_transcripts: Iterable[Path] = (),
    providers: Iterable[SourceProvider] = (),
    provenance_writer: Callable[[EnrichmentResolution], None] | None = None,
    embedding_provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    """Close one named deficit, searching only caller-supplied sources.

    The function is intentionally usable directly in disposable test vaults;
    the job queue is only the scheduling surface.  It never discovers a
    transcript corpus or local roots on its own.
    """
    if not deficit_id.strip():
        raise ValueError("enrichment.seek requires a named deficit_id")
    if not deficit.strip():
        raise ValueError("enrichment.seek requires a non-empty deficit")
    if not action_allowed("enrich", config):
        return {"status": "blocked", "reason": "enrichment action tier is not enabled"}
    if not entity_path.exists():
        return {"status": "failed", "reason": "entity_path_missing"}

    entity = load_markdown(entity_path)
    canonical_name = str(entity.frontmatter.get("canonical_name") or entity_path.stem)
    query = f"{canonical_name} {deficit}".strip()

    if current_transcript is None:
        candidate = vault / "transcripts" / f"{today_iso()}.md"
        if candidate.exists():
            current_transcript = candidate

    # 0A/0B: raw conversation wording gets first chance.
    transcript_hits = search_transcripts(
        query,
        vault=vault,
        db_path=db_path,
        current=current_transcript,
        historical=historical_transcripts,
        limit=int(((config or {}).get("enrichment") or {}).get("max_candidates_per_source", 5)),
        config=config,
        embedding_provider=embedding_provider,
    )
    if transcript_hits:
        hit = transcript_hits[0]
        resolution = EnrichmentResolution(
            text=hit.excerpt,
            source_type="transcript",
            source_uri=f"{hit.path}#{hit.conversation}".rstrip("#"),
            claim_key=deficit_id,
        )
        return _commit_or_pending(
            vault=vault, db_path=db_path, loop_id=loop_id, deficit_id=deficit_id,
            entity_path=entity_path, resolution=resolution,
            provenance_writer=provenance_writer,
        )

    # Ring 0: existing structured memory, through the canonical fusion path.
    result = retrieve_context(query=query, vault=vault, db_path=db_path)
    entity_rel = str(entity_path.relative_to(vault)) if entity_path.is_relative_to(vault) else str(entity_path)
    for item in result.direct_loaded:
        if item.path in {entity_rel} or item.path.startswith("open_loops/"):
            continue
        path = vault / item.path
        if not path.exists():
            continue
        doc = load_markdown(path)
        text = _resolution_excerpt(doc.body, item.summary)
        if text:
            resolution = EnrichmentResolution(
                text=text,
                source_type="vault",
                source_uri=item.path,
                claim_key=deficit_id,
            )
            return _commit_or_pending(
                vault=vault, db_path=db_path, loop_id=loop_id, deficit_id=deficit_id,
                entity_path=entity_path, resolution=resolution,
                provenance_writer=provenance_writer,
            )

    # Ring 1: providers are injected by the core research interface. Missing
    # providers are a clean miss, not a reason for a silent whole-feature exit.
    findings = search_owner_sources(
        query,
        providers=providers,
        max_results_per_source=int(((config or {}).get("enrichment") or {}).get("max_candidates_per_source", 5)),
    )
    if findings:
        finding = findings[0]
        resolution = EnrichmentResolution(
            text=finding.excerpt,
            source_type=finding.source,
            source_uri=finding.locator,
            claim_key=deficit_id,
        )
        return _commit_or_pending(
            vault=vault, db_path=db_path, loop_id=loop_id, deficit_id=deficit_id,
            entity_path=entity_path, resolution=resolution,
            provenance_writer=provenance_writer,
        )

    question = (
        f"I searched the current and historical transcripts, my vault, and configured "
        f"owner sources for {canonical_name} but could not resolve this deficit: {deficit}. "
        "Do you know the answer, or is this not worth pursuing?"
    )
    _leave_owner_question(vault, db_path, loop_id, question)
    return {"status": "needs_owner", "question": question, "ring": "owner"}


def configured_local_provider(config: dict[str, Any] | None) -> LocalRootProvider | None:
    source_cfg = ((config or {}).get("sources") or {}).get("local_files") or {}
    if not source_cfg.get("enabled"):
        return None
    roots = source_cfg.get("roots") or []
    if not roots:
        return None
    return LocalRootProvider(roots, extensions=source_cfg.get("include_extensions"))


def _commit_or_pending(
    *,
    vault: Path,
    db_path: Path | None,
    loop_id: str,
    deficit_id: str,
    entity_path: Path,
    resolution: EnrichmentResolution,
    provenance_writer: Callable[[EnrichmentResolution], None] | None,
) -> dict[str, Any]:
    try:
        if provenance_writer is not None:
            provenance_writer(resolution)
        _append_source_log(entity_path, resolution, vault=vault, db_path=db_path)
    except Exception as exc:
        pending = _write_pending(
            vault=vault, loop_id=loop_id, deficit_id=deficit_id,
            entity_path=entity_path, resolution=resolution, error=str(exc),
        )
        _mark_loop_error(vault, db_path, loop_id, f"Enrichment provenance failed; pending retry: {exc}")
        return {"status": "pending", "pending_path": str(pending), "error": str(exc)}

    _resolve_loop(vault, db_path, loop_id, resolution)
    return {
        "status": "resolved",
        "ring": resolution.source_type,
        "source_uri": resolution.source_uri,
        "resolution": resolution.text,
    }


def _append_source_log(entity_path: Path, resolution: EnrichmentResolution, *, vault: Path, db_path: Path | None) -> None:
    doc = load_markdown(entity_path)
    fm = dict(doc.frontmatter)
    log = [dict(entry) for entry in (fm.get("source_log") or []) if isinstance(entry, dict)]
    if resolution.basis == "inference":
        confidence = min(float(resolution.confidence if resolution.confidence is not None else MAX_INFERENCE_CONFIDENCE), MAX_INFERENCE_CONFIDENCE)
    else:
        confidence = resolution.confidence
    if resolution.basis == "direct_evidence":
        for entry in log:
            if entry.get("claim_key") == resolution.claim_key and entry.get("basis") == "inference":
                entry["status"] = "superseded"
                entry["superseded_by"] = resolution.source_uri
    entry: dict[str, Any] = {
        "date": today_iso(),
        "text": resolution.text[:1200],
        "folded": False,
        "source": resolution.source_uri,
        "source_uri": resolution.source_uri,
        "source_type": resolution.source_type,
        "basis": resolution.basis,
        "claim_key": resolution.claim_key,
    }
    if confidence is not None:
        entry["confidence"] = confidence
    log.append(entry)
    fm["source_log"] = log
    fm["updated"] = today_iso()
    write_markdown(entity_path, fm, doc.body)
    from .rebuild_index import reindex_record

    reindex_record(entity_path, vault, db_path)


def _write_pending(*, vault: Path, loop_id: str, deficit_id: str, entity_path: Path, resolution: EnrichmentResolution, error: str) -> Path:
    pending_dir = vault / "pending_enrichment"
    pending_dir.mkdir(parents=True, exist_ok=True)
    path = pending_dir / f"{today_iso()}-{_slug(loop_id)}-{_slug(deficit_id)}.md"
    payload = {
        "id": f"pending_enrichment.{_slug(loop_id)}.{_slug(deficit_id)}",
        "type": "pending_enrichment",
        "status": "quarantined",
        "created": today_iso(),
        "updated": today_iso(),
        "loop_id": loop_id,
        "deficit_id": deficit_id,
        "entity_path": str(entity_path),
        "source_type": resolution.source_type,
        "source_uri": resolution.source_uri,
        "basis": resolution.basis,
        "claim_key": resolution.claim_key,
        "retry_count": 0,
        "last_error": error,
    }
    write_markdown(path, payload, json.dumps({"resolution": resolution.text}, ensure_ascii=False))
    return path


def retry_pending(path: Path, *, vault: Path, db_path: Path | None) -> dict[str, Any]:
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    if fm.get("status") not in {"quarantined", "retry_wait"}:
        return {"status": "ignored", "reason": "not_pending"}
    try:
        data = json.loads(doc.body)
        resolution = EnrichmentResolution(
            text=str(data.get("resolution") or ""),
            source_type=str(fm.get("source_type") or "unknown"),
            source_uri=str(fm.get("source_uri") or ""),
            basis=str(fm.get("basis") or "direct_evidence"),
            claim_key=str(fm.get("claim_key") or ""),
        )
        _append_source_log(Path(str(fm["entity_path"])), resolution, vault=vault, db_path=db_path)
    except Exception as exc:
        count = int(fm.get("retry_count") or 0) + 1
        fm.update({"retry_count": count, "last_error": str(exc), "status": "failed" if count >= 3 else "retry_wait", "updated": today_iso()})
        write_markdown(path, fm, doc.body)
        _mark_loop_error(vault, db_path, str(fm.get("loop_id") or ""), f"Enrichment provenance retry {count}/3 failed: {exc}")
        from .log import get_logger

        get_logger(vault).error(
            "enrichment.provenance_retry_failed pending=%s retry=%s error=%s",
            path,
            count,
            exc,
        )
        raise RuntimeError(f"pending enrichment retry {count}/3 failed: {exc}") from exc
    fm.update({"status": "resolved", "updated": today_iso()})
    write_markdown(path, fm, doc.body)
    _resolve_loop(vault, db_path, str(fm.get("loop_id") or ""), resolution)
    return {"status": "resolved", "path": str(path)}


def _resolve_loop(vault: Path, db_path: Path | None, loop_id: str, resolution: EnrichmentResolution) -> None:
    path = _loop_path(vault, loop_id)
    if path is None:
        return
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    fm.update({
        "status": "resolved",
        "updated": today_iso(),
        "resolved_at": today_iso(),
        "resolved_by": f"enrichment.seek:{resolution.source_type}",
        "resolved_note": f"resolved from {resolution.source_uri}",
        "next_action": "",
        "owner_question": "",
    })
    write_markdown(path, fm, doc.body)
    try:
        from .rebuild_index import reindex_record

        reindex_record(path, vault, db_path)
    except Exception:
        pass


def _leave_owner_question(vault: Path, db_path: Path | None, loop_id: str, question: str) -> None:
    path = _loop_path(vault, loop_id)
    if path is None:
        return
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    fm.update({"owner_question": question, "next_action": "ask_owner", "updated": today_iso()})
    write_markdown(path, fm, doc.body)
    _reindex_optional(path, vault, db_path)


def _mark_loop_error(vault: Path, db_path: Path | None, loop_id: str, note: str) -> None:
    path = _loop_path(vault, loop_id)
    if path is None:
        return
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    fm.update({"owner_question": note, "next_action": "enrichment_retry", "updated": today_iso()})
    write_markdown(path, fm, doc.body)
    _reindex_optional(path, vault, db_path)


def _loop_path(vault: Path, loop_id: str) -> Path | None:
    direct = vault / "open_loops" / f"{loop_id}.md"
    if direct.exists():
        return direct
    for path in (vault / "open_loops").glob("*.md"):
        try:
            if str(load_markdown(path).frontmatter.get("id") or "") == loop_id:
                return path
        except Exception:
            continue
    return None


def _reindex_optional(path: Path, vault: Path, db_path: Path | None) -> None:
    try:
        from .rebuild_index import reindex_record

        reindex_record(path, vault, db_path, quiet=True)
    except Exception:
        pass


def _resolution_excerpt(body: str, summary: str) -> str:
    text = " ".join(body.split()).strip()
    return (text or str(summary or "")).strip()[:1200]


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")[:70] or "unknown"
