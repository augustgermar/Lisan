from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from ..paths import sqlite_path, vault_root


def detect_decay_candidates(vault: Path | None = None, db_path: Path | None = None) -> str:
    """Surface confidence decay candidates using deterministic SQL rules (spec §10.2)."""
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    if not db_path.exists():
        return "SQLite index not found.\n"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    lines = ["# Confidence Decay Candidates", "", f"generated: {date.today().isoformat()}", ""]
    found_any = False
    try:
        # Rule 1: last_confirmed > 180 days, confidence medium or high
        rows = conn.execute(
            """
            SELECT id, type, summary, confidence, last_confirmed FROM files
            WHERE confidence IN ('medium', 'high')
              AND (last_confirmed IS NULL OR last_confirmed < date('now', '-180 days'))
              AND status = 'active'
            ORDER BY last_confirmed ASC
            """
        ).fetchall()
        if rows:
            found_any = True
            lines.append("## Rule 1 — No recent confirmation (>180 days)")
            lines.append("Consider downgrading confidence one level.")
            for row in rows:
                lines.append(f"- `{row['id']}` | {row['type']} | {row['confidence']} | last_confirmed={row['last_confirmed']} | {row['summary'][:80]}")
            lines.append("")

        # Rule 2: disputed or unresolved claims older than 90 days
        rows = conn.execute(
            """
            SELECT c.id, c.claim_type, c.confidence, c.status, c.created, f.id AS file_id, f.summary AS file_summary
            FROM claims c JOIN files f ON c.file_id = f.id
            WHERE (c.status='unresolved' AND c.created < date('now', '-90 days'))
               OR c.status='disputed'
            ORDER BY c.created ASC
            """
        ).fetchall()
        if rows:
            found_any = True
            lines.append("## Rule 2 — Disputed or long-unresolved claims")
            lines.append("These claims should be downgraded to `disputed` confidence.")
            for row in rows:
                lines.append(f"- `{row['id']}` | {row['claim_type']} | {row['confidence']} | {row['status']} | file: `{row['file_id']}`")
            lines.append("")

        # Rule 3: stale state files (past TTL) — confidence should be 'stale'
        from ..frontmatter import load_markdown
        stale_states = []
        for path in sorted((vault / "state").glob("*.md")):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            ttl = int(doc.frontmatter.get("ttl_days", 0) or 0)
            updated = doc.frontmatter.get("updated")
            confidence = str(doc.frontmatter.get("confidence", "low"))
            if not ttl or not updated:
                continue
            try:
                age = (date.today() - date.fromisoformat(str(updated))).days
            except ValueError:
                continue
            if age > ttl and confidence != "stale":
                stale_states.append((path.name, age, ttl, confidence))
        if stale_states:
            found_any = True
            lines.append("## Rule 3 — Stale state files")
            lines.append("Confidence should be set to `stale` for these files.")
            for name, age, ttl, conf in stale_states:
                lines.append(f"- `{name}` | age={age} | ttl={ttl} | current_confidence={conf}")
            lines.append("")

        # Rule 4: high-significance episodes older than 2 years with no re-confirmation
        rows = conn.execute(
            """
            SELECT id, type, summary, confidence, last_confirmed, created FROM files
            WHERE type = 'episode'
              AND significance = 'high'
              AND confidence = 'high'
              AND (last_confirmed IS NULL OR last_confirmed < date('now', '-730 days'))
              AND created < date('now', '-730 days')
              AND status = 'active'
            ORDER BY created ASC
            """
        ).fetchall()
        if rows:
            found_any = True
            lines.append("## Rule 4 — High-confidence episodes older than 2 years, no re-confirmation")
            lines.append("Consider downgrading from `high` to `medium`.")
            for row in rows:
                lines.append(f"- `{row['id']}` | created={row['created']} | last_confirmed={row['last_confirmed']} | {row['summary'][:80]}")
            lines.append("")

        if not found_any:
            lines.append("No decay candidates found.")
            lines.append("")

    finally:
        conn.close()

    return "\n".join(lines).rstrip() + "\n"
