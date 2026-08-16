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
from .research import LocalRootProvider, SourceFinding, SourceProvider, search_owner_sources, search_published_sources
from .retrieval import retrieve_context
from .transcript_lane import TranscriptHit, search_transcripts


MAX_INFERENCE_CONFIDENCE = 0.6
_AUDIT_REL = "reports/enrichment-audit.jsonl"


def _record_attempt(
    vault: Path,
    *,
    loop_id: str,
    deficit_id: str,
    entity_path: Path,
    terminal_outcome: str,
    stop_ring: str,
    source_uri: str = "",
    error: str = "",
) -> None:
    """Append a bounded, durable outcome record without copying source text.

    The audit answers whether enrichment worked and where it stopped.  It is
    deliberately metadata-only: the resolution belongs in the entity's
    source_log, while email/transcript/file contents must not be duplicated in
    an operational rollup.
    """
    path = vault / _AUDIT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": today_iso(),
        "loop_id": loop_id,
        "deficit_id": deficit_id,
        "entity_path": str(entity_path),
        "terminal_outcome": terminal_outcome,
        "stop_ring": stop_ring,
    }
    if source_uri:
        row["source_uri"] = source_uri
    if error:
        row["error"] = str(error)[:500]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _attempts_today(vault: Path) -> int:
    path = vault / _AUDIT_REL
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if str(row.get("date") or "")[:10] == today_iso():
            count += 1
    return count


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
    published_providers: Iterable[SourceProvider] = (),
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
        _record_attempt(
            vault, loop_id=loop_id, deficit_id=deficit_id, entity_path=entity_path,
            terminal_outcome="blocked_by_policy", stop_ring="policy",
        )
        return {"status": "blocked", "reason": "enrichment action tier is not enabled"}
    if not entity_path.exists():
        _record_attempt(
            vault, loop_id=loop_id, deficit_id=deficit_id, entity_path=entity_path,
            terminal_outcome="failed", stop_ring="input", error="entity_path_missing",
        )
        return {"status": "failed", "reason": "entity_path_missing"}

    enrichment_cfg = (config or {}).get("enrichment") or {}
    daily_cap = int(enrichment_cfg.get("daily_cap", 2))
    if _attempts_today(vault) >= max(0, daily_cap):
        _record_attempt(
            vault, loop_id=loop_id, deficit_id=deficit_id, entity_path=entity_path,
            terminal_outcome="budget_exhausted", stop_ring="budget",
        )
        return {
            "status": "budget_exhausted",
            "reason": "daily enrichment cap reached",
            "daily_cap": daily_cap,
        }

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

    # Ring 2: published-world search is a separate, explicitly enabled
    # provider lane. It receives the same named deficit and bounded candidate
    # budget; it never scans the web in the background.
    published_findings = search_published_sources(
        query,
        providers=published_providers,
        max_results_per_source=int(enrichment_cfg.get("max_candidates_per_source", 5)),
    )
    if published_findings:
        finding = published_findings[0]
        resolution = EnrichmentResolution(
            text=finding.excerpt,
            source_type="web",
            source_uri=finding.locator,
            claim_key=deficit_id,
            confidence=finding.confidence,
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
    _leave_owner_question(
        vault, db_path, loop_id, question,
        deficit_id=deficit_id, entity_path=entity_path,
    )
    _record_attempt(
        vault, loop_id=loop_id, deficit_id=deficit_id, entity_path=entity_path,
        terminal_outcome="unresolved", stop_ring="owner",
    )
    return {"status": "needs_owner", "question": question, "ring": "owner"}


def configured_local_provider(config: dict[str, Any] | None) -> LocalRootProvider | None:
    source_cfg = ((config or {}).get("sources") or {}).get("local_files") or {}
    if not source_cfg.get("enabled"):
        return None
    roots = source_cfg.get("roots") or []
    if not roots:
        return None
    return LocalRootProvider(roots, extensions=source_cfg.get("include_extensions"))


def resolve_owner_clarification(
    *,
    vault: Path,
    text: str,
    conversation_id: str | None,
    transcript_path: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Resolve the one enrichment question delivered in this conversation.

    This seam is intentionally deterministic and narrow. It will not attach
    an arbitrary owner turn to an old loop: the drive stamps the conversation
    that received the question, and only that exact conversation can answer it.
    """
    if not str(text or "").strip() or not conversation_id:
        return None
    pending: tuple[Path, dict[str, Any]] | None = None
    for path in sorted((vault / "open_loops").glob("*.md")):
        try:
            fm = dict(load_markdown(path).frontmatter)
        except Exception:
            continue
        if (
            str(fm.get("type") or "") == "open_loop"
            and str(fm.get("status") or "") == "active"
            and str(fm.get("next_action") or "") == "ask_owner"
            and str(fm.get("owner_inquiry_conversation_id") or "") == str(conversation_id)
        ):
            if pending is not None:
                # Ambiguous ownership is safer than guessing which deficit the
                # sentence answers.
                return {"status": "ambiguous", "reason": "multiple_owner_inquiries"}
            pending = (path, fm)
    if pending is None:
        return None

    path, fm = pending
    classification = _classify_owner_response(text)
    source_uri = str(transcript_path or "")
    if not source_uri:
        source_uri = f"transcripts/{conversation_id}"
    outcome = classification["terminal_outcome"]
    loop_doc = load_markdown(path)
    loop_updates = {
        **dict(loop_doc.frontmatter),
        "updated": today_iso(),
        "enrichment_terminal_outcome": outcome,
        "enrichment_stop_ring": "owner",
        "owner_response_class": classification["source_type"],
        "owner_response_provenance": source_uri,
        "owner_question": "",
        "next_action": "",
    }

    entity_path = _owner_entity_path(vault, fm)
    if classification["source_type"] in {"boundary", "decline", "not_important"}:
        loop_updates["status"] = "resolved"
        loop_updates["resolved_at"] = today_iso()
        loop_updates["resolved_by"] = "owner_interaction"
        loop_updates["resolved_note"] = classification["note"]
        write_markdown(path, loop_updates, loop_doc.body)
        _reindex_optional(path, vault, db_path)
    elif entity_path is not None:
        resolution = EnrichmentResolution(
            text=str(text).strip(), source_type="owner_interaction", source_uri=source_uri,
            basis="direct_owner_statement", claim_key=str(fm.get("enrichment_deficit_id") or fm.get("id") or ""),
        )
        _append_source_log(entity_path, resolution, vault=vault, db_path=db_path)
        loop_updates.update({
            "status": "resolved", "resolved_at": today_iso(),
            "resolved_by": "owner_interaction", "resolved_note": "owner clarification recorded",
        })
        write_markdown(path, loop_updates, loop_doc.body)
        _reindex_optional(path, vault, db_path)
    else:
        # Preserve a useful owner answer even when an older loop has no entity
        # link; the loop remains the durable record of the clarification.
        loop_updates.update({
            "status": "resolved", "resolved_at": today_iso(),
            "resolved_by": "owner_interaction", "resolved_note": str(text).strip()[:1200],
        })
        write_markdown(path, loop_updates, loop_doc.body)
        _reindex_optional(path, vault, db_path)

    _record_attempt(
        vault, loop_id=str(fm.get("id") or path.stem),
        deficit_id=str(fm.get("enrichment_deficit_id") or fm.get("id") or ""),
        entity_path=entity_path or path, terminal_outcome=outcome, stop_ring="owner",
        source_uri=source_uri,
    )
    return {
        "status": "resolved",
        "terminal_outcome": outcome,
        "source_type": classification["source_type"],
        "loop_id": str(fm.get("id") or path.stem),
    }


def _classify_owner_response(text: str) -> dict[str, str]:
    lowered = " ".join(str(text).lower().split())
    if any(p in lowered for p in ("don't research", "do not research", "never research", "don't look into")):
        return {"source_type": "boundary", "terminal_outcome": "owner_declined", "note": "owner set a research boundary"}
    if any(p in lowered for p in ("not worth pursuing", "not important", "drop it", "leave it open", "don't bother")):
        return {"source_type": "not_important", "terminal_outcome": "owner_marked_not_important", "note": "owner marked the deficit not important"}
    if any(p in lowered for p in ("i don't know", "no idea", "can't answer", "cannot answer", "i'm not sure")):
        return {"source_type": "decline", "terminal_outcome": "owner_declined", "note": "owner declined or could not answer"}
    if lowered.startswith(("actually", "correction", "that's wrong", "that is wrong", "no, ")):
        return {"source_type": "correction", "terminal_outcome": "resolved_by_owner", "note": "owner supplied a correction"}
    if any(p in lowered for p in ("i prefer", "i want", "i'd rather", "i would rather")):
        return {"source_type": "preference", "terminal_outcome": "resolved_by_owner", "note": "owner supplied a preference"}
    if any(p in lowered for p in ("i think", "probably", "seems like", "my guess")):
        return {"source_type": "interpretation", "terminal_outcome": "resolved_by_owner", "note": "owner supplied an interpretation"}
    return {"source_type": "fact", "terminal_outcome": "resolved_by_owner", "note": "owner supplied a direct statement"}


def _owner_entity_path(vault: Path, fm: dict[str, Any]) -> Path | None:
    explicit = str(fm.get("enrichment_entity_path") or "").strip()
    candidates = [explicit] + [str(link) for link in (fm.get("links") or [])]
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            path = vault / path
        if path.exists() and path.is_file() and str(load_markdown(path).frontmatter.get("type") or "") == "entity":
            return path
    return None


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
        _record_attempt(
            vault, loop_id=loop_id, deficit_id=deficit_id, entity_path=entity_path,
            terminal_outcome="pending_provenance", stop_ring=resolution.source_type,
            source_uri=resolution.source_uri, error=str(exc),
        )
        return {"status": "pending", "pending_path": str(pending), "error": str(exc)}

    _resolve_loop(vault, db_path, loop_id, resolution)
    outcome = {
        "transcript": "resolved_by_transcript",
        "vault": "resolved_by_vault",
        "gmail_search": "resolved_by_local_source",
        "obsidian_search": "resolved_by_local_source",
        "local_files": "resolved_by_local_source",
        "web": "resolved_by_web",
    }.get(resolution.source_type, "resolved_by_local_source")
    _record_attempt(
        vault, loop_id=loop_id, deficit_id=deficit_id, entity_path=entity_path,
        terminal_outcome=outcome, stop_ring=resolution.source_type,
        source_uri=resolution.source_uri,
    )
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
        _record_attempt(
            vault, loop_id=str(fm.get("loop_id") or ""), deficit_id=str(fm.get("deficit_id") or ""),
            entity_path=Path(str(fm.get("entity_path") or "")),
            terminal_outcome="failed", stop_ring=str(fm.get("source_type") or "provenance"),
            source_uri=str(fm.get("source_uri") or ""), error=str(exc),
        )
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
    _record_attempt(
        vault, loop_id=str(fm.get("loop_id") or ""), deficit_id=str(fm.get("deficit_id") or ""),
        entity_path=Path(str(fm.get("entity_path") or "")),
        terminal_outcome={
            "transcript": "resolved_by_transcript", "vault": "resolved_by_vault",
            "gmail_search": "resolved_by_local_source", "obsidian_search": "resolved_by_local_source",
        "local_files": "resolved_by_local_source",
        "web": "resolved_by_web",
        }.get(resolution.source_type, "resolved_by_local_source"),
        stop_ring=resolution.source_type, source_uri=resolution.source_uri,
    )
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
        "enrichment_terminal_outcome": {
            "transcript": "resolved_by_transcript",
            "vault": "resolved_by_vault",
            "gmail_search": "resolved_by_local_source",
            "obsidian_search": "resolved_by_local_source",
            "local_files": "resolved_by_local_source",
            "web": "resolved_by_web",
        }.get(resolution.source_type, "resolved_by_local_source"),
        "enrichment_stop_ring": resolution.source_type,
        "next_action": "",
        "owner_question": "",
    })
    write_markdown(path, fm, doc.body)
    try:
        from .rebuild_index import reindex_record

        reindex_record(path, vault, db_path)
    except Exception:
        pass


def _leave_owner_question(
    vault: Path, db_path: Path | None, loop_id: str, question: str,
    *, deficit_id: str = "", entity_path: Path | None = None,
) -> None:
    path = _loop_path(vault, loop_id)
    if path is None:
        return
    doc = load_markdown(path)
    fm = dict(doc.frontmatter)
    fm.update({
        "owner_question": question,
        "next_action": "ask_owner",
        "enrichment_terminal_outcome": "unresolved",
        "enrichment_stop_ring": "owner",
        "enrichment_deficit_id": deficit_id,
        "enrichment_entity_path": str(entity_path or ""),
        "updated": today_iso(),
    })
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
