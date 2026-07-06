"""Safe entity merging: absorb a fragment into its true entity.

The scale test showed entities fragmenting into base + qualified variants
("deck rebuild" / "deck rebuild project (summer 2026)"). Prevention now
happens at birth (entity_resolution binds suffix-qualified proposals to
the base), and this module closes the other half: merging fragments that
already exist.

A merge never destroys data:
- the fragment's narrative and source_log entries are appended to the
  survivor's durable source_log (dated, provenance-marked);
- the fragment's names become the survivor's aliases, so every future
  mention binds to the survivor;
- the fragment file itself moves to archive/entities/ (reversible);
- one compaction job re-tells the survivor's story with the new material.

Ambiguous candidates are never merged automatically — they surface as
question-phrased deviations for the owner ("are these the same thing?"),
consistent with the surface-with-curiosity contradiction policy.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..utils import today_iso
from .log import get_logger


def merge_entities(
    vault: Path,
    source: str,
    target: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Merge entity *source* (name or id) into *target*. Returns a summary.
    Refuses identity merges and missing entities; never guesses."""
    src = _find_entity(vault, source)
    dst = _find_entity(vault, target)
    if src is None:
        return {"merged": False, "reason": f"source entity not found: {source}"}
    if dst is None:
        return {"merged": False, "reason": f"target entity not found: {target}"}
    if src == dst:
        return {"merged": False, "reason": "source and target are the same entity"}

    src_doc = load_markdown(src)
    dst_doc = load_markdown(dst)
    src_fm = dict(src_doc.frontmatter)
    dst_fm = dict(dst_doc.frontmatter)
    src_name = str(src_fm.get("canonical_name") or src.stem)
    dst_name = str(dst_fm.get("canonical_name") or dst.stem)

    # 1. absorb content into the survivor's durable log
    log = [dict(e) for e in (dst_fm.get("source_log") or []) if isinstance(e, dict)]
    src_body = re.sub(r"^#\s+.*$", "", src_doc.body, count=1, flags=re.M).strip()
    if src_body:
        log.append({
            "date": today_iso(),
            "text": f"(merged from duplicate entity '{src_name}') " + re.sub(r"\s+", " ", src_body)[:2400],
            "folded": False,
            "source": f"merge:{src.stem}",
        })
    for entry in (src_fm.get("source_log") or []):
        if isinstance(entry, dict) and entry.get("text"):
            carried = dict(entry)
            carried["folded"] = False
            carried.setdefault("source", f"merge:{src.stem}")
            log.append(carried)
    dst_fm["source_log"] = log

    # 2. names: the fragment's identity becomes reachable aliases
    aliases = {str(a).strip() for a in (dst_fm.get("aliases") or []) if str(a).strip()}
    aliases.add(src_name)
    aliases.update(str(a).strip() for a in (src_fm.get("aliases") or []) if str(a).strip())
    aliases.discard(dst_name)
    dst_fm["aliases"] = sorted(aliases)
    dst_fm["updated"] = today_iso()
    write_markdown(dst, dst_fm, dst_doc.body)

    # 3. the fragment file retires to the archive (reversible)
    archive = vault / "archive" / "entities"
    archive.mkdir(parents=True, exist_ok=True)
    archived_path = archive / f"merged-{src.stem}.md"
    shutil.move(str(src), str(archived_path))

    # 4. reindex + one compaction to weave the absorbed material in
    _reindex(vault, db_path, dst, removed=src)
    try:
        from .jobs import enqueue_job

        enqueue_job("entity.rewrite_story",
                    {"entity_path": str(dst), "force_compact": True}, db_path=db_path)
    except Exception:
        pass

    get_logger(vault).info(f"entity.merged source={src.stem} target={dst.stem}")
    return {
        "merged": True,
        "source": src_name,
        "target": dst_name,
        "archived": str(archived_path),
        "log_entries_carried": len(log),
    }


def dedup_candidates(vault: Path) -> list[dict[str, Any]]:
    """Same-kind near-duplicate pairs worth asking about: one canonical name
    is a token-subset or qualifier-variant of another. Reported, never
    auto-merged — the owner (or the agent in conversation, with the owner's
    yes) decides."""
    from .entity_resolution import _qualifier_base

    ents = []
    root = vault / "entities"
    if not root.exists():
        return []
    for p in sorted(root.rglob("*.md")):
        try:
            fm = load_markdown(p).frontmatter
        except Exception:
            continue
        name = str(fm.get("canonical_name") or "").strip()
        kind = str(fm.get("kind") or fm.get("subtype") or "").strip()
        if name:
            ents.append({"path": p, "name": name, "kind": kind})

    out = []
    for i, a in enumerate(ents):
        at = set(a["name"].lower().split())
        for b in ents[i + 1:]:
            if a["kind"] != b["kind"]:
                continue
            bt = set(b["name"].lower().split())
            subset = (at < bt or bt < at) and bool(at & bt)
            same_base = _qualifier_base(a["name"]) == _qualifier_base(b["name"]) and a["name"].lower() != b["name"].lower()
            exact = a["name"].lower() == b["name"].lower()
            if subset or same_base or exact:
                smaller, larger = (a, b) if len(at) <= len(bt) else (b, a)
                out.append({
                    "keep": smaller["name"], "absorb": larger["name"],
                    "kind": a["kind"],
                    "why": "exact duplicate" if exact else ("same base name" if same_base else "name is a token-subset"),
                })
    return out


def _find_entity(vault: Path, ref: str) -> Path | None:
    ref = str(ref or "").strip()
    if not ref:
        return None
    root = vault / "entities"
    if not root.exists():
        return None
    ref_lower = ref.lower()
    for p in sorted(root.rglob("*.md")):
        try:
            fm = load_markdown(p).frontmatter
        except Exception:
            continue
        names = {str(fm.get("canonical_name") or "").lower(), str(fm.get("id") or "").lower(), p.stem.lower()}
        names.update(str(a).lower() for a in (fm.get("aliases") or []))
        if ref_lower in names:
            return p
    return None


def _reindex(vault: Path, db_path: Path | None, updated: Path, *, removed: Path) -> None:
    try:
        from .rebuild_index import index_single_record, open_index_connection

        conn = open_index_connection(db_path)
        try:
            index_single_record(updated, vault, conn)
            conn.execute("DELETE FROM files WHERE path = ?", (str(removed.relative_to(vault)),))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
