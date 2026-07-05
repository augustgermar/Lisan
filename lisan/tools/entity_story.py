"""entity_story — durable log + periodic compaction for entity narratives.

The design, chosen after a scale test showed per-turn full-rewrite was both
lossy (it dropped the arc's ending under length pressure) and the throughput
bottleneck at volume:

- Every mention APPENDS the new material to an append-only ``source_log`` in
  the entity's frontmatter. This is cheap (no LLM), deterministic, and the
  durable ground truth — entries are never deleted, only marked ``folded``
  once compaction has woven them into the narrative.
- COMPACTION runs only when enough unfolded material has accumulated (or on
  demand): one writer call re-tells the narrative core from the prior story
  plus the unfolded log. Rare and batched instead of per-turn.
- The whole ``source_log`` is included in the search index, so a fact that
  compaction judged unimportant and left out of the prose is still findable.
  Compaction is lossy only in the *rendering*, never in the *storage* — the
  narrative is a regenerable view over a log that keeps everything.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .deixis import tokenize_principal
from .rebuild_index import index_single_record, open_index_connection

# Compact after this many unfolded log entries have accumulated. Overridable
# via config (memory.entity_compact_threshold).
_DEFAULT_COMPACT_THRESHOLD = 3
_LOG_ENTRY_MAX_CHARS = 1200


def _compact_threshold() -> int:
    try:
        from ..config import load_config

        v = int((load_config().get("memory") or {}).get("entity_compact_threshold") or _DEFAULT_COMPACT_THRESHOLD)
        return max(1, v)
    except Exception:
        return _DEFAULT_COMPACT_THRESHOLD


def rewrite_entity_story(
    vault: Path,
    entity_path: Path,
    *,
    draft_path: Path | None = None,
    transcript_path: Path | None = None,
    conversation_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    db_path: Path | None = None,
    force_compact: bool = False,
) -> dict[str, Any]:
    """Append this turn's material to the entity's durable log, and compact the
    narrative when enough has accumulated. Returns a summary dict for the job
    dispatcher."""
    if not entity_path.exists():
        return {"updated": False, "reason": "entity_path_missing", "entity_path": str(entity_path)}

    doc = load_markdown(entity_path)
    fm = dict(doc.frontmatter)
    prior_body = doc.body

    new_material = _read_draft_body(draft_path) or _read_transcript_tail(transcript_path)

    log = [dict(e) for e in (fm.get("source_log") or []) if isinstance(e, dict)]
    appended = False
    if new_material:
        entry = _condense(new_material)
        if entry and (not log or log[-1].get("text") != entry):
            log.append({"date": today_iso(), "text": entry, "folded": False})
            appended = True
    fm["source_log"] = log

    unfolded = [e for e in log if not e.get("folded")]
    should_compact = force_compact or len(unfolded) >= _compact_threshold()

    if not should_compact:
        if not appended:
            return {"updated": False, "reason": "no_new_material", "entity_path": str(entity_path)}
        # Persist the appended log and index it now — the new material is
        # searchable immediately, even before it is woven into the prose.
        write_markdown(entity_path, fm, prior_body)
        _reindex(entity_path, vault, db_path)
        return {"updated": True, "action": "appended", "unfolded": len(unfolded),
                "entity_path": str(entity_path)}

    if not unfolded:
        return {"updated": False, "reason": "nothing_to_compact", "entity_path": str(entity_path)}

    return _compact(
        vault=vault, entity_path=entity_path, fm=fm, prior_body=prior_body,
        log=log, unfolded=unfolded, provider=provider, model=model, db_path=db_path,
    )


def _compact(
    *,
    vault: Path,
    entity_path: Path,
    fm: dict[str, Any],
    prior_body: str,
    log: list[dict[str, Any]],
    unfolded: list[dict[str, Any]],
    provider: str | None,
    model: str | None,
    db_path: Path | None,
) -> dict[str, Any]:
    """Re-tell the narrative core from the prior story plus the unfolded log,
    then mark those entries folded (never deleted). Guarded so a compaction can
    never return a materially shorter story than it replaced."""
    from ..agents.writer import WriterAgent
    from ..schemas import get_schema

    canonical_name = str(fm.get("canonical_name") or fm.get("id") or entity_path.stem)
    prior_story = _strip_title(prior_body).strip()
    new_material = "\n\n".join(f"({e.get('date')}) {e.get('text')}" for e in unfolded)

    entity_meta = "\n".join([
        f"canonical_name: {canonical_name}",
        f"kind: {fm.get('kind') or fm.get('subtype') or 'thing'}",
        f"summary: {fm.get('summary', '')}",
        f"significance: {fm.get('significance', 'low')}",
    ])

    agent = WriterAgent(vault=vault)
    result = agent.run_json(
        new_material,
        task="entity_story",
        schema=get_schema("entity_story_output"),
        provider=provider,
        model=model,
        entity_frontmatter=entity_meta,
        prior_story=prior_story or "(no prior story — first narrative write)",
        today=today_iso(),
    )
    narrative = str(result.get("narrative") or "").strip()
    arc_note = str(result.get("arc_note") or "").strip()
    if not narrative:
        return {"updated": False, "reason": "empty_narrative", "entity_path": str(entity_path)}

    # No-shrink guardrail: a compaction that folds in new material must never
    # come back materially shorter than the story it replaces. If it does, keep
    # the fuller prior story and append the new developments — the durable log
    # already holds everything, so this only protects the readable prose.
    prior_words = len(prior_story.split())
    new_words = len(narrative.split())
    if prior_words >= 60 and new_words < prior_words * 0.85:
        appended = _append_developments(prior_story, new_material)
        if appended:
            narrative = appended
            arc_note = (arc_note + " [guardrail: rewrite shrank; kept prior + appended new]").strip()
        else:
            return {"updated": False, "reason": "rewrite_shrank_story",
                    "prior_words": prior_words, "new_words": new_words,
                    "entity_path": str(entity_path)}

    narrative = tokenize_principal(narrative, vault)
    # Mark the compacted entries folded — kept, not deleted.
    for entry in log:
        if not entry.get("folded"):
            entry["folded"] = True
    fm["source_log"] = log
    fm["updated"] = today_iso()
    write_markdown(entity_path, fm, f"# {canonical_name}\n\n{narrative}\n")
    _reindex(entity_path, vault, db_path)
    return {"updated": True, "action": "compacted", "arc_note": arc_note,
            "folded": len(unfolded), "entity_path": str(entity_path)}


def entity_search_text(fm: dict[str, Any], body: str) -> str:
    """The full searchable text for an entity: the narrative core plus every
    entry in the durable log. Ensures a logged fact is findable even when the
    compacted prose left it out."""
    parts = [_strip_title(body).strip()]
    for entry in (fm.get("source_log") or []):
        if isinstance(entry, dict) and entry.get("text"):
            parts.append(str(entry["text"]))
    return "\n\n".join(p for p in parts if p).strip()


def _condense(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_LOG_ENTRY_MAX_CHARS]


def _strip_title(body: str) -> str:
    return re.sub(r"^#\s+.*$", "", body, count=1, flags=re.M)


def _append_developments(prior_story: str, new_material: str) -> str:
    body = _strip_title(prior_story).strip()
    if not body:
        return ""
    addition = re.sub(r"\s+", " ", new_material).strip()[:900]
    return f"{body}\n\n{addition}" if addition else body


def _reindex(entity_path: Path, vault: Path, db_path: Path | None) -> None:
    conn = open_index_connection(db_path)
    try:
        index_single_record(entity_path, vault, conn)
        conn.commit()
    finally:
        conn.close()


def _read_draft_body(draft_path: Path | None) -> str:
    if not draft_path:
        return ""
    try:
        path = Path(draft_path)
        if not path.exists():
            return ""
        return load_markdown(path).body.strip()
    except Exception:
        return ""


def _read_transcript_tail(transcript_path: Path | None, chars: int = 4000) -> str:
    if not transcript_path:
        return ""
    try:
        path = Path(transcript_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")[-chars:].strip()
    except Exception:
        return ""
