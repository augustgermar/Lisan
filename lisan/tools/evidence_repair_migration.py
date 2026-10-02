"""Operator-only repair and incremental release of quarantined evidence.

This migration is deliberately not exposed through the Lisan CLI, jobs,
Adjutant, or self-repair.  It consumes an owner-reviewed successor map,
canonicalizes only exact references, preserves everything else as explicit
provenance, and releases one record from the retrieval quarantine only after
the repaired record validates and has been reindexed.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..frontmatter import dump_markdown, load_markdown, parse_markdown
from .db import connect
from .rebuild_index import reindex_record
from .record_quarantine import ensure_record_quarantine_table
from .record_refs import build_reference_index, resolve_reference
from .validator import validate_record_candidate, validate_vault
from .write_boundary import write_if_structured_record


MIGRATION_ID = "20261001-step3-evidence-repair"
BACKUP_PATH = "/Users/august/.lisan/backups/lisan-backup-20261001-071151.tar.gz.enc"
BACKUP_SHA256 = "c409e11a3769c78f4eb57471aa1d8e6056257308734a1cdeaaa556aa13289f27"

# The owner approved these mappings after reviewing the exact promotion
# evidence.  Four mechanically-derived candidates were explicitly excluded
# because the semantics did not identify the successor uniquely.
EXCLUDED_SUCCESSORS: dict[str, str] = {
    "2026-08-07": "Bare date denotes a day containing multiple episodes, not one record.",
    "2026-09-01": "Bare date denotes a day containing multiple episodes, not one record.",
    "episode.2026-08-14": "Generic day-level episode id does not uniquely identify the promoted event.",
    "2026-08-07 SDP spending-plan completion check-in": (
        "Owner rejected the proposed domestic-check-in successor as a semantic mismatch; "
        "retain for separate review of Marisol's SDP service evidence."
    ),
}

DUPLICATE_ARCHIVES = {
    "evidence/records/2026-09-18-jake-s-clarification-of-google-drive-access-on-2026-08-20.md": {
        "canonical": "evidence/records/2026-08-20-jake-s-clarification-of-google-drive-access-on-2026-08-20.md",
        "archive": "archive/evidence/superseded-duplicate-2026-09-18-jake-s-clarification-of-google-drive-access-on-2026-08-20.md",
    },
    "evidence/records/2026-09-18-jake-s-failed-attempt-to-write-a-test-file-to-the-user-s-local-google-drive-folder.md": {
        "canonical": "evidence/records/2026-07-18-jake-s-failed-attempt-to-write-a-test-file-to-the-user-s-local-google-drive-folder.md",
        "archive": "archive/evidence/superseded-duplicate-2026-09-18-jake-s-failed-attempt-to-write-a-test-file-to-the-user-s-local-google-drive-folder.md",
    },
}


class EvidenceMigrationError(RuntimeError):
    pass


def prepare_migration(
    vault: Path,
    db_path: Path,
    quarantine_manifest_path: Path,
    mapping_review_path: Path,
    migration_root: Path,
) -> dict[str, Any]:
    """Create byte-for-byte before/after artifacts without touching the vault."""
    migration_root.mkdir(parents=True, exist_ok=True)
    metadata_path = migration_root / "migration.json"
    if metadata_path.exists():
        raise EvidenceMigrationError(f"migration is already prepared: {metadata_path}")

    quarantine = json.loads(quarantine_manifest_path.read_text(encoding="utf-8"))
    mapping_review = json.loads(mapping_review_path.read_text(encoding="utf-8"))
    evidence_rows = [row for row in quarantine.get("records", []) if row.get("type") == "evidence"]
    if len(evidence_rows) != 41:
        raise EvidenceMigrationError(f"expected 41 quarantined evidence records, found {len(evidence_rows)}")

    reviewed = {
        str(row["old_target"]): str(row["proposed_successor"])
        for row in mapping_review.get("mappings", [])
    }
    approved = {key: value for key, value in reviewed.items() if key not in EXCLUDED_SUCCESSORS}
    if len(approved) != 23:
        raise EvidenceMigrationError(f"expected 23 approved mappings after exclusions, found {len(approved)}")

    reference_index = build_reference_index(vault)
    before_root = migration_root / "before"
    after_root = migration_root / "after"
    records: list[dict[str, Any]] = []
    diff_parts: list[str] = []

    for row in evidence_rows:
        relative = str(row["path"])
        source = _inside(vault, relative)
        if not source.is_file():
            raise EvidenceMigrationError(f"quarantined evidence is missing: {relative}")
        before_bytes = source.read_bytes()
        if _sha_bytes(before_bytes) != str(row["content_sha256"]):
            raise EvidenceMigrationError(f"quarantine hash drift: {relative}")
        mode = stat.S_IMODE(source.stat().st_mode)

        if relative in DUPLICATE_ARCHIVES:
            info = DUPLICATE_ARCHIVES[relative]
            canonical = _inside(vault, info["canonical"])
            archive = _inside(vault, info["archive"])
            if not canonical.is_file():
                raise EvidenceMigrationError(f"canonical duplicate survivor missing: {canonical}")
            if archive.exists():
                raise EvidenceMigrationError(f"archive destination already exists: {archive}")
            canonical_before = canonical.read_bytes()
            canonical_document = load_markdown(canonical)
            canonical_frontmatter, counts = _transform_frontmatter(
                canonical_document.frontmatter,
                vault=vault,
                reference_index=reference_index,
                approved_mappings=approved,
            )
            canonical_rendered = dump_markdown(canonical_frontmatter, canonical_document.body)
            canonical_after = canonical_rendered.encode("utf-8")
            candidate = validate_record_candidate(
                canonical,
                canonical_frontmatter,
                canonical_document.body,
                vault=vault,
                reference_index=reference_index,
            )
            # The later copy has not been archived during prepare, so exactly
            # that known duplicate error is expected. Every other defect must
            # already be gone from the survivor's prepared form.
            errors = [issue.message for issue in candidate.issues if issue.severity == "error"]
            allowed_duplicate = f"Duplicate id {row['id']} also used in {source}"
            unexpected = [message for message in errors if message != allowed_duplicate]
            if unexpected or errors.count(allowed_duplicate) != 1:
                raise EvidenceMigrationError(
                    f"canonical duplicate survivor is not otherwise clean: {canonical}: {errors}"
                )
            record = {
                "action": "archive_duplicate",
                "path": relative,
                "archive_path": info["archive"],
                "canonical_path": info["canonical"],
                "record_id": row["id"],
                "mode": mode,
                "before_sha256": _sha_bytes(before_bytes),
                "after_sha256": _sha_bytes(before_bytes),
                "canonical_mode": stat.S_IMODE(canonical.stat().st_mode),
                "canonical_before_sha256": _sha_bytes(canonical_before),
                "canonical_after_sha256": _sha_bytes(canonical_after),
                **counts,
            }
            records.append(record)
            _write_bytes(before_root / relative, before_bytes, 0o600)
            _write_bytes(after_root / info["archive"], before_bytes, 0o600)
            _write_bytes(before_root / info["canonical"], canonical_before, 0o600)
            _write_bytes(after_root / info["canonical"], canonical_after, 0o600)
            diff_parts.extend(
                difflib.unified_diff(
                    before_bytes.decode("utf-8").splitlines(keepends=True),
                    [],
                    fromfile=f"a/{relative}",
                    tofile="/dev/null",
                )
            )
            diff_parts.extend(
                difflib.unified_diff(
                    [],
                    before_bytes.decode("utf-8").splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=f"b/{info['archive']}",
                )
            )
            diff_parts.extend(
                difflib.unified_diff(
                    canonical_before.decode("utf-8").splitlines(keepends=True),
                    canonical_rendered.splitlines(keepends=True),
                    fromfile=f"a/{info['canonical']}",
                    tofile=f"b/{info['canonical']}",
                )
            )
            continue

        document = load_markdown(source)
        transformed, counts = _transform_frontmatter(
            document.frontmatter,
            vault=vault,
            reference_index=reference_index,
            approved_mappings=approved,
        )
        rendered = dump_markdown(transformed, document.body)
        candidate = validate_record_candidate(
            source,
            transformed,
            document.body,
            vault=vault,
            reference_index=reference_index,
        )
        errors = [issue.message for issue in candidate.issues if issue.severity == "error"]
        if errors:
            raise EvidenceMigrationError(f"prepared candidate remains invalid: {relative}: {errors}")

        before_text = before_bytes.decode("utf-8")
        after_bytes = rendered.encode("utf-8")
        _write_bytes(before_root / relative, before_bytes, 0o600)
        _write_bytes(after_root / relative, after_bytes, 0o600)
        diff_parts.extend(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        records.append(
            {
                "action": "repair",
                "path": relative,
                "record_id": row["id"],
                "mode": mode,
                "before_sha256": _sha_bytes(before_bytes),
                "after_sha256": _sha_bytes(after_bytes),
                **counts,
            }
        )

    records = _dependency_order(records, migration_root, vault)
    prior_overlay = _overlay_rows(db_path, [str(row["path"]) for row in evidence_rows])
    if len(prior_overlay) != 41 or any(int(row["active"]) != 1 for row in prior_overlay.values()):
        raise EvidenceMigrationError("all 41 evidence records must still be actively quarantined before prepare")

    shutil.copy2(quarantine_manifest_path, migration_root / "quarantine-manifest-at-prepare.json")
    shutil.copy2(mapping_review_path, migration_root / "owner-reviewed-mapping.json")
    (migration_root / "applied.diff").write_text("".join(diff_parts), encoding="utf-8")
    os.chmod(migration_root / "applied.diff", 0o600)
    metadata = {
        "format": 1,
        "migration_id": MIGRATION_ID,
        "state": "prepared",
        "prepared_at": _now(),
        "vault": str(vault.resolve()),
        "database": str(db_path.resolve()),
        "restore_anchor": {"path": BACKUP_PATH, "sha256": BACKUP_SHA256, "predates_migration": True},
        "evidence_record_count": len(records),
        "approved_mapping_count": len(approved),
        "approved_mapping_instances": sum(int(row["approved_mapping_count"]) for row in records),
        "excluded_mapping_count": len(EXCLUDED_SUCCESSORS),
        "excluded_mappings": EXCLUDED_SUCCESSORS,
        "records": records,
        "prior_quarantine_rows": prior_overlay,
        "released": [],
        "failed": [],
    }
    _write_json_atomic(metadata_path, metadata)
    return metadata


def apply_prepared_migration(vault: Path, db_path: Path, migration_root: Path) -> dict[str, Any]:
    """Apply and release each prepared record; restore that record on failure."""
    metadata_path = migration_root / "migration.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("state") != "prepared":
        raise EvidenceMigrationError(f"migration is not prepared: {metadata.get('state')}")
    if Path(str(metadata["vault"])).resolve() != vault.resolve():
        raise EvidenceMigrationError("prepared migration belongs to another vault")
    if Path(str(metadata["database"])).resolve() != db_path.resolve():
        raise EvidenceMigrationError("prepared migration belongs to another database")

    for record in metadata["records"]:
        source = _inside(vault, record["path"])
        if not source.is_file() or _sha_bytes(source.read_bytes()) != record["before_sha256"]:
            raise EvidenceMigrationError(f"pre-apply drift: {record['path']}")
        if record["action"] == "archive_duplicate":
            canonical = _inside(vault, record["canonical_path"])
            if (
                not canonical.is_file()
                or _sha_bytes(canonical.read_bytes()) != record["canonical_before_sha256"]
            ):
                raise EvidenceMigrationError(f"pre-apply canonical drift: {record['canonical_path']}")

    metadata["state"] = "applying"
    metadata["apply_started_at"] = _now()
    _write_json_atomic(metadata_path, metadata)

    for position, record in enumerate(metadata["records"], 1):
        try:
            if record["action"] == "archive_duplicate":
                _apply_archive_record(vault, db_path, migration_root, record)
            else:
                _apply_repair_record(vault, db_path, migration_root, record)
            if record.get("release_quarantine", True):
                _deactivate_quarantine(db_path, record["path"])
            release = {
                "position": position,
                "path": record["path"],
                "record_id": record["record_id"],
                "action": record["action"],
                "released_at": _now(),
            }
            metadata["released"].append(release)
            _write_json_atomic(metadata_path, metadata)
            print(
                f"released {position}/{len(metadata['records'])}: "
                f"{record['record_id']} ({record['action']})",
                flush=True,
            )
        except Exception as exc:
            _restore_one(vault, db_path, migration_root, record, metadata["prior_quarantine_rows"])
            metadata["failed"].append(
                {"position": position, "path": record["path"], "error": str(exc), "failed_at": _now()}
            )
            metadata["state"] = "failed"
            _write_json_atomic(metadata_path, metadata)
            raise EvidenceMigrationError(
                f"record {position} failed and was restored: {record['path']}: {exc}"
            ) from exc

    report = validate_vault(vault, db_path=db_path)
    metadata["state"] = "applied"
    metadata["applied_at"] = _now()
    metadata["validator_after_apply"] = _report_counts(report)
    _write_json_atomic(metadata_path, metadata)
    return metadata


def rollback_migration(vault: Path, db_path: Path, migration_root: Path) -> dict[str, Any]:
    """Restore all 41 files and quarantine rows as one hash-guarded unit."""
    metadata_path = migration_root / "migration.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("state") not in {"applied", "failed"}:
        raise EvidenceMigrationError(f"migration cannot be rolled back from {metadata.get('state')}")

    for record in reversed(metadata.get("released", [])):
        full = next(row for row in metadata["records"] if row["path"] == record["path"])
        _restore_one(vault, db_path, migration_root, full, metadata["prior_quarantine_rows"])

    metadata["state"] = "rolled_back"
    metadata["rolled_back_at"] = _now()
    _write_json_atomic(metadata_path, metadata)
    return metadata


def _transform_frontmatter(
    frontmatter: dict[str, Any],
    *,
    vault: Path,
    reference_index,
    approved_mappings: dict[str, str],
) -> tuple[dict[str, Any], dict[str, int]]:
    transformed = json.loads(json.dumps(frontmatter))
    provenance: list[dict[str, Any]] = list(transformed.get("reference_provenance") or [])
    counts = {
        "approved_mapping_count": 0,
        "canonical_reference_count": 0,
        "source_locator_count": 0,
        "known_ambiguous_count": 0,
    }

    resolved_fields: dict[str, list[str]] = {}
    for field_name in ("links", "linked_claims", "linked_episodes"):
        values = transformed.get(field_name)
        if not isinstance(values, list):
            continue
        kept: list[str] = []
        for value in values:
            target, note = _resolve_legacy_reference(
                value,
                field_name=field_name,
                vault=vault,
                reference_index=reference_index,
                approved_mappings=approved_mappings,
            )
            if target:
                if target not in kept:
                    kept.append(target)
                if isinstance(value, str) and value in approved_mappings:
                    counts["approved_mapping_count"] += 1
                elif target != value:
                    counts["canonical_reference_count"] += 1
                continue
            if note and note not in provenance:
                provenance.append(note)
                key = "source_locator_count" if note["status"] == "source_locator" else "known_ambiguous_count"
                counts[key] += 1
        resolved_fields[field_name] = kept

    for name, values in resolved_fields.items():
        transformed[name] = values

    # The broad graph field must include every proven typed edge; provenance
    # never returns to it. Preserve order from the original links first.
    links = list(transformed.get("links") or [])
    for field_name in ("linked_claims", "linked_episodes"):
        for target in transformed.get(field_name) or []:
            if target not in links:
                links.append(target)
    transformed["links"] = links
    if provenance:
        transformed["reference_provenance"] = provenance
    transformed["reference_repair"] = {
        "migration": MIGRATION_ID,
        "policy": "exact evidence only; semantic mismatch overrides mechanical promotion",
        "reviewed_by": "owner",
    }
    return transformed, counts


def _resolve_legacy_reference(
    value: Any,
    *,
    field_name: str,
    vault: Path,
    reference_index,
    approved_mappings: dict[str, str],
) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(value, str) or not value.strip():
        return None, _provenance(field_name, value, "known_ambiguous", "Not a non-empty record reference.")
    text = value.strip()
    if text in EXCLUDED_SUCCESSORS:
        return None, _provenance(
            field_name,
            text,
            "known_ambiguous",
            EXCLUDED_SUCCESSORS[text],
            owner_decision=True,
        )
    if text in approved_mappings:
        return approved_mappings[text], None

    # An absolute path outside the vault is a source locator. A matching
    # basename elsewhere in the vault is not evidence that the two artifacts
    # are the same (the Psyhacks collision exposed this exact trap).
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        try:
            relative = candidate.resolve(strict=False).relative_to(vault.resolve())
        except ValueError:
            return None, _provenance(
                field_name,
                text,
                "source_locator" if candidate.exists() else "known_ambiguous",
                "External filesystem locator; basename similarity is not a semantic identity proof.",
            )
        resolution = resolve_reference(relative.as_posix(), vault, reference_index)
    else:
        resolution = resolve_reference(text, vault, reference_index)

    if resolution.ok:
        return resolution.target, None
    if resolution.repairable:
        # Exact claim text and exact in-vault path/filename identity are safe.
        # Merge forwarding is excluded here unless the owner-reviewed map
        # already handled it above.
        if resolution.kind == "merged":
            return None, _provenance(
                field_name,
                text,
                "known_ambiguous",
                f"Merged successor {resolution.target} was not owner-approved for this batch.",
            )
        return resolution.target, None
    if resolution.kind == "unindexed":
        return None, _provenance(field_name, text, "source_locator", resolution.detail)
    return None, _provenance(field_name, text, "known_ambiguous", resolution.detail)


def _provenance(
    field_name: str,
    value: Any,
    status: str,
    reason: str,
    *,
    owner_decision: bool = False,
) -> dict[str, Any]:
    note: dict[str, Any] = {
        "field": field_name,
        "original_value": value,
        "status": status,
        "reason": reason,
        "migration": MIGRATION_ID,
    }
    if owner_decision:
        note["owner_decision"] = "excluded_from_successor_mapping_on_2026-10-01"
    return note


def _dependency_order(records: list[dict[str, Any]], migration_root: Path, vault: Path) -> list[dict[str, Any]]:
    archives = [row for row in records if row["action"] == "archive_duplicate"]
    repairs = [row for row in records if row["action"] == "repair"]
    by_id = {str(row["record_id"]): row for row in repairs}
    dependencies: dict[str, set[str]] = {str(row["path"]): set() for row in repairs}
    for row in repairs:
        after = parse_markdown((migration_root / "after" / row["path"]).read_text(encoding="utf-8"))
        for target in after.frontmatter.get("links") or []:
            if target in by_id and target != row["record_id"]:
                dependencies[row["path"]].add(by_id[target]["path"])

    ordered: list[dict[str, Any]] = []
    remaining = {str(row["path"]): row for row in repairs}
    while remaining:
        ready = sorted(path for path in remaining if not (dependencies[path] & remaining.keys()))
        if not ready:
            # Cycles are valid reference structures; deterministic path order
            # is safe because target files already exist, only visibility lags.
            ready = [sorted(remaining)[0]]
        for path in ready:
            ordered.append(remaining.pop(path))
    return sorted(archives, key=lambda row: row["path"]) + ordered


def _apply_repair_record(vault: Path, db_path: Path, migration_root: Path, record: dict[str, Any]) -> None:
    path = _inside(vault, record["path"])
    prepared = (migration_root / "after" / record["path"]).read_text(encoding="utf-8")
    document = parse_markdown(prepared)
    if not write_if_structured_record(
        path,
        document.frontmatter,
        document.body,
        prepared,
        db_path=db_path,
    ):
        raise EvidenceMigrationError(f"prepared evidence path was not recognized as structured: {path}")
    if _sha_bytes(path.read_bytes()) != record["after_sha256"]:
        raise EvidenceMigrationError(f"post-write hash mismatch: {record['path']}")
    candidate = validate_record_candidate(path, document.frontmatter, document.body, vault=vault)
    errors = [issue.message for issue in candidate.issues if issue.severity == "error"]
    if errors:
        raise EvidenceMigrationError(f"record did not validate after write: {errors}")
    reindex_record(path, vault, db_path)
    for relative in record.get("reindex_after", []) or []:
        reindex_record(_inside(vault, str(relative)), vault, db_path)


def _apply_archive_record(vault: Path, db_path: Path, migration_root: Path, record: dict[str, Any]) -> None:
    source = _inside(vault, record["path"])
    archive = _inside(vault, record["archive_path"])
    canonical = _inside(vault, record["canonical_path"])
    archive.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, archive)
    if _sha_bytes(archive.read_bytes()) != record["after_sha256"]:
        raise EvidenceMigrationError(f"archive hash mismatch: {record['archive_path']}")
    # The pre-Step-2 index can point at the later duplicate. Move the indexed
    # identity to the survivor before asking the reservation ledger to admit
    # an update at the canonical path. The quarantine key still hides the id
    # during this brief intermediate state.
    reindex_record(canonical, vault, db_path, remove=source)
    prepared = (migration_root / "after" / record["canonical_path"]).read_text(encoding="utf-8")
    canonical_doc = parse_markdown(prepared)
    if not write_if_structured_record(
        canonical,
        canonical_doc.frontmatter,
        canonical_doc.body,
        prepared,
        db_path=db_path,
    ):
        raise EvidenceMigrationError(f"canonical survivor was not recognized as structured: {canonical}")
    if _sha_bytes(canonical.read_bytes()) != record["canonical_after_sha256"]:
        raise EvidenceMigrationError(f"canonical survivor hash mismatch: {record['canonical_path']}")
    candidate = validate_record_candidate(canonical, canonical_doc.frontmatter, canonical_doc.body, vault=vault)
    errors = [issue.message for issue in candidate.issues if issue.severity == "error"]
    if errors:
        raise EvidenceMigrationError(f"canonical duplicate survivor remains invalid: {errors}")
    reindex_record(canonical, vault, db_path, remove=source)


def _restore_one(
    vault: Path,
    db_path: Path,
    migration_root: Path,
    record: dict[str, Any],
    prior_rows: dict[str, dict[str, Any]],
) -> None:
    source = _inside(vault, record["path"])
    before = (migration_root / "before" / record["path"]).read_bytes()
    if record["action"] == "archive_duplicate":
        archive = _inside(vault, record["archive_path"])
        if archive.exists():
            archive.unlink()
        canonical = _inside(vault, record["canonical_path"])
        canonical_before = (migration_root / "before" / record["canonical_path"]).read_bytes()
        _atomic_write_bytes(canonical, canonical_before, int(record["canonical_mode"]))
    _atomic_write_bytes(source, before, int(record["mode"]))
    cleanup_id = str(record.get("remove_index_id_on_restore") or "").strip()
    if cleanup_id:
        _remove_index_id(db_path, cleanup_id)
    prior = prior_rows.get(record["path"])
    if prior is not None:
        _restore_quarantine_row(db_path, prior)
    try:
        reindex_record(source, vault, db_path)
        for relative in record.get("reindex_after", []) or []:
            reindex_record(_inside(vault, str(relative)), vault, db_path)
    except Exception:
        pass


def _deactivate_quarantine(db_path: Path, relative: str) -> None:
    conn = connect(db_path)
    try:
        ensure_record_quarantine_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE record_quarantine SET active = 0 WHERE record_path = ? AND active = 1",
            (relative,),
        ).rowcount
        if changed != 1:
            raise EvidenceMigrationError(f"expected one active quarantine row for {relative}, changed {changed}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _overlay_rows(db_path: Path, paths: list[str]) -> dict[str, dict[str, Any]]:
    conn = connect(db_path)
    try:
        ensure_record_quarantine_table(conn)
        rows = conn.execute("SELECT * FROM record_quarantine").fetchall()
        wanted = set(paths)
        return {str(row["record_path"]): dict(row) for row in rows if str(row["record_path"]) in wanted}
    finally:
        conn.close()


def _restore_quarantine_row(db_path: Path, row: dict[str, Any]) -> None:
    columns = [
        "record_path", "record_id", "record_type", "reason", "issue_count",
        "issue_fingerprints", "content_sha256", "migration_id", "quarantined_at", "active",
    ]
    conn = connect(db_path)
    try:
        ensure_record_quarantine_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"INSERT OR REPLACE INTO record_quarantine ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[name] for name in columns],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _remove_index_id(db_path: Path, record_id: str) -> None:
    """Remove a migration-created id before reindexing its restored predecessor."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM links WHERE source_id = ?", (record_id,))
        conn.execute("DELETE FROM links WHERE target_id = ?", (record_id,))
        try:
            conn.execute("DELETE FROM files_fts WHERE id = ?", (record_id,))
        except Exception:
            pass
        conn.execute("DELETE FROM files WHERE id = ?", (record_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _report_counts(report) -> dict[str, int]:
    return {
        "errors": sum(1 for issue in report.issues if issue.severity == "error"),
        "warnings": sum(1 for issue in report.issues if issue.severity == "warning"),
    }


def _inside(vault: Path, relative: str) -> Path:
    root = vault.resolve()
    path = (root / relative).resolve(strict=False)
    if path == root or root not in path.parents:
        raise EvidenceMigrationError(f"path escapes vault: {relative}")
    return path


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_bytes(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    os.chmod(path, mode)


def _atomic_write_bytes(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    rendered = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, rendered, 0o600)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
