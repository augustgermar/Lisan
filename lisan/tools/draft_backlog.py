"""One-shot resolver for the pre-auto-promotion draft backlog.

Before episode auto-promotion existed, skeptic-approved episode drafts
accumulated at ``status: fanout_applied`` with nowhere to go — the episodic
layer of the vault stayed empty while its content sat in the review queue's
blind spot. This walks the backlog and promotes what the pipeline would
have promoted, using the Writer JSON preserved in each draft. Drafts of
fanned-out tasks (entity/state/open_loop/decision) are left as they are:
their records already exist; promoting them again would duplicate.

Exposed as ``lisan draft promote-backlog``. Idempotent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown, write_markdown
from ..paths import sqlite_path, vault_root
from .drafts import promote_episode_from_writer, writer_output_from_draft
from .log import log_error
from .rebuild_index import index_record_best_effort


def promote_backlog(vault: Path | None = None, db_path: Path | None = None) -> dict[str, Any]:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    stats = {"scanned": 0, "promoted": 0, "already_promoted": 0, "skipped": 0, "errors": 0}
    for path in sorted((vault / "drafts").glob("*.md")):
        stats["scanned"] += 1
        try:
            doc = load_markdown(path)
        except Exception:
            stats["errors"] += 1
            continue
        fm = dict(doc.frontmatter)
        pipeline = fm.get("pipeline") if isinstance(fm.get("pipeline"), dict) else {}
        task = str(pipeline.get("task") or "episode")
        status = str(fm.get("status") or "")
        if task != "episode" or status != "fanout_applied" or not fm.get("skeptic_approved"):
            stats["skipped"] += 1
            continue
        writer = writer_output_from_draft(doc)
        if not writer:
            stats["errors"] += 1
            continue
        created = str(fm.get("created") or "")[:10]
        # Draft filenames carry the capture date; frontmatter 'created' has
        # been observed stale (a 2026-05-22 default under a 2026-07-03
        # filename). Prefer the filename date when it parses.
        name_date = path.name[:10]
        if len(name_date) == 10 and name_date[4] == "-" and name_date[7] == "-":
            created = name_date
        try:
            mode = str(pipeline.get("mode") or "extraction")
            promoted = promote_episode_from_writer(
                vault,
                writer=writer,
                draft_path=path,
                created=created or "2026-01-01",
                source=mode if mode in ("elicitor", "extraction") else "extraction",
            )
        except Exception as exc:
            log_error(vault, f"draft backlog promotion failed for {path.name}", exc)
            stats["errors"] += 1
            continue
        if promoted is None:
            stats["already_promoted"] += 1
            continue
        index_record_best_effort(vault, promoted, db_path)
        fm["status"] = "promoted"
        fm["promoted_to"] = str(promoted.relative_to(vault))
        write_markdown(path, fm, doc.body)
        stats["promoted"] += 1
    return stats
