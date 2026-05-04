from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..paths import sqlite_path, vault_root


def generate_health_report(vault: Path | None = None, db_path: Path | None = None) -> str:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        lines = ["# Memory Health Report", ""]
        if not db_path.exists():
            lines.append("SQLite index not found.")
            return "\n".join(lines) + "\n"

        stale_states = []
        for path in (vault / "state").glob("*.md"):
            try:
                doc = load_markdown(path)
            except Exception:
                continue
            ttl = int(doc.frontmatter.get("ttl_days", 0) or 0)
            updated = doc.frontmatter.get("updated")
            if ttl and updated:
                try:
                    age = (date.today() - date.fromisoformat(str(updated))).days
                except ValueError:
                    continue
                if age > ttl:
                    stale_states.append((path.name, age, ttl))

        lines.append("## Stale State")
        if stale_states:
            for name, age, ttl in stale_states:
                lines.append(f"- `{name}` is {age} day(s) old, TTL {ttl}")
        else:
            lines.append("- None")
        lines.append("")

        active_loops = conn.execute("SELECT id, summary, review_after FROM files WHERE type='open_loop' AND status='active'").fetchall()
        lines.append("## Open Loops")
        if active_loops:
            for row in active_loops:
                lines.append(f"- `{row['id']}` | {row['summary']} | review_after={row['review_after']}")
        else:
            lines.append("- None")
        lines.append("")

        contradictions = (vault / "contradictions")
        lines.append("## Contradictions")
        contradiction_files = sorted(contradictions.glob("*.md"))
        if contradiction_files:
            for path in contradiction_files:
                lines.append(f"- `{path.name}`")
        else:
            lines.append("- None")
        lines.append("")

        lines.append("## Claims")
        claim_rows = conn.execute(
            "SELECT id, claim_type, confidence, status, created, review_after FROM claims ORDER BY created DESC"
        ).fetchall()
        if claim_rows:
            for row in claim_rows:
                lines.append(
                    f"- `{row['id']}` | {row['claim_type']} | {row['confidence']} | {row['status']} | review_after={row['review_after']}"
                )
        else:
            lines.append("- None")
        lines.append("")

        lines.append("## Manifest Cap")
        manifest_core = vault / "manifests" / "manifest-core.md"
        if manifest_core.exists():
            entry_count = sum(
                1 for line in manifest_core.read_text(encoding="utf-8").splitlines() if line.startswith("- ")
            )
            lines.append(f"- manifest-core entries: {entry_count}/200")
        else:
            lines.append("- manifest-core not generated")
        lines.append("")

        lines.append("## Backup Status")
        backup = vault / "backup.md"
        if backup.exists():
            age_days = (date.today() - date.fromtimestamp(backup.stat().st_mtime)).days
            lines.append(f"- backup.md last modified {age_days} day(s) ago")
        else:
            lines.append("- backup.md missing")
        lines.append("")

        lines.append("## Index Counts")
        counts = {
            "files": conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "claims": conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0],
            "aliases": conn.execute("SELECT COUNT(*) FROM entity_aliases").fetchone()[0],
            "epochs": conn.execute("SELECT COUNT(*) FROM entity_epochs").fetchone()[0],
        }
        for key, value in counts.items():
            lines.append(f"- {key}: {value}")

        return "\n".join(lines).rstrip() + "\n"
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    report = generate_health_report()
    out = vault_root() / "reports" / f"health-{date.today().isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    print(str(out))
    return 0
