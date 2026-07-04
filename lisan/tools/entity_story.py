"""entity_story — arc-preserving narrative rewrite for entity records.

Called as an async queue job (entity.rewrite_story). Reads the entity's
current story and the new episode material, rewrites the narrative via the
writer agent, tokenizes the result, and writes it back through the normal
write+index seam.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .deixis import tokenize_principal
from .rebuild_index import index_single_record, open_index_connection


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
) -> dict[str, Any]:
    """Arc-preserving entity narrative rewrite.

    Loads the entity's current story and new episode material (from
    *draft_path* or *transcript_path*), calls the writer agent, tokenizes the
    output so no real names leak into storage, and writes the result back to
    *entity_path*. Re-indexes immediately so semantic retrieval sees the
    updated story without a separate `lisan jobs run`.

    Returns a summary dict with ``updated`` (bool), ``arc_note``, and
    ``entity_path`` (str) so the job dispatcher has something to log.
    """
    from ..agents.writer import WriterAgent
    from ..schemas import get_schema

    if not entity_path.exists():
        return {"updated": False, "reason": "entity_path_missing", "entity_path": str(entity_path)}

    doc = load_markdown(entity_path)
    fm = dict(doc.frontmatter)
    prior_story = doc.body.strip()
    canonical_name = str(fm.get("canonical_name") or fm.get("id") or entity_path.stem)

    # Gather new material: prefer the episode draft, fall back to transcript.
    new_material = _read_draft_body(draft_path)
    if not new_material:
        new_material = _read_transcript_tail(transcript_path)
    if not new_material:
        return {"updated": False, "reason": "no_new_material", "entity_path": str(entity_path)}

    entity_meta = "\n".join([
        f"canonical_name: {canonical_name}",
        f"kind: {fm.get('kind') or fm.get('subtype') or 'thing'}",
        f"summary: {fm.get('summary', '')}",
        f"significance: {fm.get('significance', 'low')}",
    ])

    agent = WriterAgent(vault=vault)
    schema = get_schema("entity_story_output")
    result = agent.run_json(
        new_material,
        task="entity_story",
        schema=schema,
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

    # No-shrink guardrail (deterministic-first): a rewrite that folds in new
    # material must not come back materially shorter than the story it
    # replaces — that is silent content loss, the "act three deleted" failure.
    # The model handles prose; this code guarantees the arc is never
    # compressed away. When the rewrite shrinks meaningfully, keep the fuller
    # prior story and append the new developments rather than overwrite.
    prior_words = len(prior_story.split())
    new_words = len(narrative.split())
    if prior_words >= 60 and new_words < prior_words * 0.85:
        appended = _append_developments(prior_story, new_material, canonical_name)
        if appended:
            narrative = appended
            arc_note = (arc_note + " [guardrail: rewrite shrank the story; kept prior and appended new developments]").strip()
        else:
            return {
                "updated": False,
                "reason": "rewrite_shrank_story",
                "prior_words": prior_words,
                "new_words": new_words,
                "entity_path": str(entity_path),
            }

    # Tokenize: no real principal name must reach the vault file.
    narrative = tokenize_principal(narrative, vault)

    new_body = f"# {canonical_name}\n\n{narrative}\n"
    fm["updated"] = today_iso()
    write_markdown(entity_path, fm, new_body)

    conn = open_index_connection(db_path)
    try:
        index_single_record(entity_path, vault, conn)
        conn.commit()
    finally:
        conn.close()

    return {
        "updated": True,
        "arc_note": arc_note,
        "entity_path": str(entity_path),
    }


def _append_developments(prior_story: str, new_material: str, canonical_name: str) -> str:
    """Fallback when a rewrite would shrink the story: keep the established
    narrative verbatim and add the new material as a continuation paragraph,
    so nothing established is ever lost. Returns "" if there is no prior body
    to preserve."""
    import re

    body = re.sub(r"^#\s+.*$", "", prior_story, count=1, flags=re.M).strip()
    if not body:
        return ""
    addition = new_material.strip()
    if not addition:
        return body
    # keep the addition compact — a single continuation paragraph
    addition = re.sub(r"\s+", " ", addition)[:800].strip()
    return f"{body}\n\n{addition}"



def _read_draft_body(draft_path: Path | None) -> str:
    if not draft_path:
        return ""
    try:
        path = Path(draft_path)
        if not path.exists():
            return ""
        doc = load_markdown(path)
        return doc.body.strip()
    except Exception:
        return ""


def _read_transcript_tail(transcript_path: Path | None, chars: int = 4000) -> str:
    if not transcript_path:
        return ""
    try:
        path = Path(transcript_path)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[-chars:].strip()
    except Exception:
        return ""
