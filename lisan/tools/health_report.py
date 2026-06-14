from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..frontmatter import load_markdown
from ..paths import sqlite_path, vault_root
from .deixis import render_for_display
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
                lines.append(f"- `{fid}` | {render_for_display(summary, vault)} | review_after={review_after}{flag}")
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
                lines.append(f"- `{row['id']}` | {render_for_display(row['summary'], vault)} | last_confirmed={row['last_confirmed']}")
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

        lines.append("")
        lines.append("## Embeddings")
        lines.extend(_embeddings_health_lines(vault, db_path, conn))

        drafts_dir = vault / "drafts"
        if drafts_dir.exists():
            all_drafts = list(drafts_dir.glob("*.md"))
            skeptic_blocked = [p for p in all_drafts if "skeptic_blocked" in p.name or "needs_revision" in p.name]
            lines.append("")
            lines.append("## Draft Backlog")
            lines.append(f"- total drafts on disk: {len(all_drafts)}")
            if skeptic_blocked:
                lines.append(f"- skeptic-blocked (not indexed): {len(skeptic_blocked)}")
                lines.append("  These turns were captured but not promoted to durable records.")
            else:
                lines.append("- skeptic-blocked: 0")

        body = "\n".join(lines).rstrip() + "\n"
        return _render_report(frontmatter, body)
    finally:
        conn.close()


def _embeddings_health_lines(vault: Path, db_path: Path, conn: sqlite3.Connection) -> list[str]:
    """Surface embedding state: active mode, embedder reachability, the index's
    stored model + dimension, and how many records are still pending."""
    from ..config import embedding_settings, load_config
    from ..providers.embeddings import EmbeddingProvider
    from .vector_store import load_index

    lines: list[str] = []
    config = load_config()
    settings = embedding_settings(config)
    lines.append(f"- active mode: {settings.get('mode')} (provider={settings.get('provider')})")
    lines.append(f"- unreachable_policy: {settings.get('unreachable_policy')}")

    # The embed attempt is the reachability probe — no separate ping.
    reachable = "n/a (mode=hash)"
    if settings.get("mode") != "hash":
        probe = EmbeddingProvider(config).embed_query("healthcheck")
        reachable = "yes" if probe.reachable else "no"
    lines.append(f"- embedder reachable: {reachable}")

    index = load_index(db_path.parent / "embeddings.bin")
    if index.vectors:
        lines.append(f"- index model: {index.model} | dimension: {index.dimension} | vectors: {len(index.vectors)}")
    else:
        lines.append("- index: no vectors written (run `lisan rebuild-index`)")

    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM files WHERE COALESCE(embedding_status, 'pending') = 'pending'"
        ).fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM files WHERE embedding_status IN ('embedded', 'hash')"
        ).fetchone()[0]
        lines.append(f"- records embedded: {embedded} | pending: {pending}")
        if pending:
            lines.append("  Run `lisan sync` or the `index.embed_pending` job to drain pending records.")
    except sqlite3.Error:
        lines.append("- embedding_status column not available (rebuild index)")
    return lines


def main(argv: list[str] | None = None) -> int:
    report = generate_health_report()
    out = vault_root() / "reports" / f"health-{date.today().isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    print(str(out))
    return 0


def _render_report(frontmatter: dict[str, Any], body: str) -> str:
    return "---\n" + json.dumps(frontmatter, indent=2, ensure_ascii=True) + "\n---\n\n" + body
