from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..paths import sqlite_path, vault_root
from .deixis import render_for_display
from .narrative_state import load_narrative_state


@dataclass(slots=True)
class BatchReviewItem:
    category: str
    identifier: str
    summary: str
    due: str
    path: str


def generate_batch_review(vault: Path | None = None, db_path: Path | None = None) -> str:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    today = date.today().isoformat()

    items = _collect_items(vault, db_path)
    frontmatter = {
        "id": f"report.batch-review.{today}",
        "type": "report",
        "created": today,
        "updated": today,
        "status": "active",
        "significance": "low",
        "domain_primary": "cross_arena",
        "domain_secondary": [],
        "privacy": "personal",
        "compartments": [],
        "allowed_contexts": ["all"],
        "blocked_contexts": [],
        "summary": "Batch review digest",
        "links": [],
        "confidence": "low",
        "confidence_basis": "Generated batch review digest",
        "last_confirmed": today,
        "review_after": today,
        "generated": today,
    }
    lines = ["# Batch Review Digest", "", f"generated: {today}", ""]
    if not items:
        lines.append("No review items are currently due.")
        lines.append("")
        body = "\n".join(lines).rstrip() + "\n"
        return _render_report(frontmatter, body)

    grouped: dict[str, list[BatchReviewItem]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)

    order = ["state", "draft", "open_loop", "conversation", "claim"]
    for category in order:
        if category not in grouped:
            continue
        lines.append(f"## {category.title()}s")
        for item in grouped[category]:
            lines.append(f"- `{item.identifier}` | {render_for_display(item.summary, vault)} | due={item.due} | `{item.path}`")
        lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    return _render_report(frontmatter, body)


def write_batch_review(vault: Path | None = None, db_path: Path | None = None) -> Path:
    vault = vault or vault_root()
    out = vault / "reports" / f"batch-review-{date.today().isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate_batch_review(vault, db_path), encoding="utf-8")
    return out


def _render_report(frontmatter: dict[str, Any], body: str) -> str:
    return f"---\n{json.dumps(frontmatter, indent=2, ensure_ascii=True)}\n---\n\n{body.rstrip()}\n"


def _collect_items(vault: Path, db_path: Path) -> list[BatchReviewItem]:
    items: list[BatchReviewItem] = []
    items.extend(_stale_states(vault))
    items.extend(_due_open_loops(vault, db_path))
    items.extend(_due_claims(db_path))
    items.extend(_pending_drafts(vault))
    items.extend(_active_conversations(vault))
    items.extend(_stale_backup(vault))
    return items


def _stale_states(vault: Path) -> list[BatchReviewItem]:
    items: list[BatchReviewItem] = []
    for path in sorted((vault / "state").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        ttl = int(doc.frontmatter.get("ttl_days", 0) or 0)
        updated = doc.frontmatter.get("updated")
        if not ttl or not updated:
            continue
        try:
            age = (date.today() - date.fromisoformat(str(updated))).days
        except ValueError:
            continue
        if age > ttl:
            items.append(
                BatchReviewItem(
                    category="state",
                    identifier=str(doc.frontmatter.get("id", path.stem)),
                    summary=str(doc.frontmatter.get("summary", path.stem)),
                    due=str(doc.frontmatter.get("review_after", updated)),
                    path=str(path.relative_to(vault)),
                )
            )
    return items


def _due_open_loops(vault: Path, db_path: Path) -> list[BatchReviewItem]:
    items: list[BatchReviewItem] = []
    if not db_path.exists():
        return items
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, summary, review_after, path FROM files WHERE type='open_loop' AND status='active'"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        review_after = str(row["review_after"] or "")
        if review_after:
            try:
                if date.today() < date.fromisoformat(review_after):
                    continue
            except ValueError:
                continue
        items.append(
            BatchReviewItem(
                category="open_loop",
                identifier=str(row["id"]),
                summary=str(row["summary"]),
                due=review_after or "now",
                path=str(row["path"]),
            )
        )
    return items


def _due_claims(db_path: Path) -> list[BatchReviewItem]:
    items: list[BatchReviewItem] = []
    if not db_path.exists():
        return items
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, claim_text, claim_type, confidence, status, review_after, episode_id
            FROM claims
            WHERE status IN ('unresolved', 'disputed')
            """
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        review_after = str(row["review_after"] or "")
        if review_after:
            try:
                if date.today() < date.fromisoformat(review_after):
                    continue
            except ValueError:
                continue
        items.append(
            BatchReviewItem(
                category="claim",
                identifier=str(row["id"]),
                summary=f"{row['claim_type']} | {row['claim_text']}",
                due=review_after or "now",
                path=str(row["episode_id"]),
            )
        )
    return items


def _pending_drafts(vault: Path) -> list[BatchReviewItem]:
    items: list[BatchReviewItem] = []
    for path in sorted((vault / "drafts").glob("*.md")):
        try:
            doc = load_markdown(path)
        except Exception:
            continue
        status = str(doc.frontmatter.get("status", "") or "")
        if status not in {"pending", "needs_revision"}:
            continue
        review_after = str(doc.frontmatter.get("review_after", ""))
        if review_after:
            try:
                if date.today() < date.fromisoformat(review_after):
                    continue
            except ValueError:
                continue
        items.append(
            BatchReviewItem(
                category="draft",
                identifier=str(doc.frontmatter.get("id", path.stem)),
                summary=str(doc.frontmatter.get("summary", path.stem)),
                due=review_after or "now",
                path=str(path.relative_to(vault)),
            )
        )
    return items


def _active_conversations(vault: Path) -> list[BatchReviewItem]:
    items: list[BatchReviewItem] = []
    for path in sorted((vault / "transcripts" / "narrative").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        state = load_narrative_state(vault, payload.get("conversation_id"))
        if state.turn_count <= 0 or state.mode_status == "closed":
            continue
        items.append(
            BatchReviewItem(
                category="conversation",
                identifier=state.conversation_id,
                summary=f"{state.story_thread or 'Active conversation'} ({state.turn_count} turn(s))",
                due=state.updated,
                path=str(path.relative_to(vault)),
            )
        )
    return items


def _stale_backup(vault: Path) -> list[BatchReviewItem]:
    backup = vault / "backup.md"
    if not backup.exists():
        return [
            BatchReviewItem(
                category="backup",
                identifier="backup.md",
                summary="No backup log exists yet.",
                due="now",
                path=str(backup.relative_to(vault)) if backup.parent == vault else "backup.md",
            )
        ]
    age_days = (date.today() - date.fromtimestamp(backup.stat().st_mtime)).days
    if age_days <= 30:
        return []
    return [
        BatchReviewItem(
            category="backup",
            identifier="backup.md",
            summary=f"Backup log is {age_days} day(s) old.",
            due=str(date.fromtimestamp(backup.stat().st_mtime).date()),
            path=str(backup.relative_to(vault)),
        )
    ]
