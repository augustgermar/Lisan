from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..paths import sqlite_path, vault_root
from .domain_fields import with_domain_fields


def generate_health_report(vault: Path | None = None, db_path: Path | None = None) -> str:
    vault = vault or vault_root()
    db_path = db_path or sqlite_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        report_date = date.today().isoformat()
        frontmatter = with_domain_fields(
            {
                "id": f"report.health.{report_date}",
                "type": "report",
                "created": report_date,
                "updated": report_date,
                "status": "active",
                "significance": "low",
                "domain_primary": "cross_arena",
                "domain_secondary": [],
                "privacy": "personal",
                "compartments": [],
                "allowed_contexts": ["all"],
                "blocked_contexts": [],
                "summary": "Memory health report",
                "links": [],
                "confidence": "low",
                "confidence_basis": "Generated memory health report",
                "last_confirmed": report_date,
                "review_after": report_date,
                "generated": report_date,
            }
        )
        lines = ["# Memory Health Report", ""]
        if not db_path.exists():
            lines.append("SQLite index not found.")
            body = "\n".join(lines).rstrip() + "\n"
            return _render_report(frontmatter, body)

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

        lines.append("## Stale Domains")
        if stale_states:
            for name, age, ttl in stale_states:
                lines.append(f"- `{name}` is {age} day(s) old, TTL {ttl}")
        else:
            lines.append("- None")
        lines.append("")

        today = date.today()
        overdue_loops = []
        for row in conn.execute("SELECT id, summary, review_after FROM files WHERE type='open_loop' AND status='active'").fetchall():
            review_after = str(row["review_after"] or "")
            overdue = False
            if review_after:
                try:
                    overdue = today > date.fromisoformat(review_after)
                except ValueError:
                    pass
            overdue_loops.append((str(row["id"]), str(row["summary"]), review_after, overdue))
        lines.append("## Open Loops")
        if overdue_loops:
            for fid, summary, review_after, overdue in overdue_loops:
                flag = " ⚠ OVERDUE" if overdue else ""
                lines.append(f"- `{fid}` | {summary} | review_after={review_after}{flag}")
        else:
            lines.append("- None")
        lines.append("")

        contradictions_dir = vault / "contradictions"
        contradiction_files = sorted(contradictions_dir.glob("*.md")) if contradictions_dir.exists() else []
        old_contradictions = []
        for path in contradiction_files:
            try:
                doc = load_markdown(path)
                created = doc.frontmatter.get("created")
                if created:
                    age = (today - date.fromisoformat(str(created))).days
                    if age > 90:
                        old_contradictions.append((path.name, age))
            except Exception:
                pass
        lines.append("## Contradictions")
        if contradiction_files:
            for path in contradiction_files:
                lines.append(f"- `{path.name}`")
            if old_contradictions:
                lines.append("")
                lines.append("  Unresolved > 90 days:")
                for name, age in old_contradictions:
                    lines.append(f"  - `{name}` ({age} days)")
        else:
            lines.append("- None")
        lines.append("")

        lines.append("## Claims")
        stuck_claim_rows = conn.execute(
            """
            SELECT id, claim_type, confidence, status, created, review_after FROM claims
            WHERE (status='unresolved' AND created < date('now', '-90 days'))
               OR (status='disputed')
               OR (status='hypothesis' AND created < date('now', '-180 days'))
            ORDER BY created ASC
            """
        ).fetchall()
        if stuck_claim_rows:
            for row in stuck_claim_rows:
                lines.append(
                    f"- `{row['id']}` | {row['claim_type']} | {row['confidence']} | {row['status']} | review_after={row['review_after']}"
                )
        else:
            lines.append("- None stuck/disputed")
        lines.append("")

        # Entities with no episodes linking to them (based on last_confirmed date)
        entity_decay = conn.execute(
            """
            SELECT id, summary, last_confirmed FROM files
            WHERE type = 'entity' AND status = 'active'
            AND (last_confirmed IS NULL OR last_confirmed < date('now', '-365 days'))
            """
        ).fetchall()
        lines.append("## Entity Decay Candidates")
        lines.append("Entities not confirmed in 365+ days:")
        if entity_decay:
            for row in entity_decay:
                lines.append(f"- `{row['id']}` | {row['summary']} | last_confirmed={row['last_confirmed']}")
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

        lines.append("## LLM Calls")
        try:
            total_calls = conn.execute("SELECT COUNT(*) FROM llm_call_log").fetchone()[0]
            recent_failures = conn.execute(
                "SELECT agent, provider, timestamp FROM llm_call_log WHERE success=0 ORDER BY timestamp DESC LIMIT 5"
            ).fetchall()
            lines.append(f"- total logged: {total_calls}")
            if recent_failures:
                lines.append("- recent failures:")
                for row in recent_failures:
                    lines.append(f"  - {row['timestamp']} | {row['agent']} | {row['provider']}")
            else:
                lines.append("- no recent failures")
        except Exception:
            lines.append("- llm_call_log not available")
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

        body = "\n".join(lines).rstrip() + "\n"
        return _render_report(frontmatter, body)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    report = generate_health_report()
    out = vault_root() / "reports" / f"health-{date.today().isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    print(str(out))
    return 0


def _render_report(frontmatter: dict[str, Any], body: str) -> str:
    return "---\n" + json.dumps(frontmatter, indent=2, ensure_ascii=True) + "\n---\n\n" + body
