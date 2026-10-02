"""Owner-approved repair of the five quarantined longitudinal patterns."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..frontmatter import dump_markdown, load_markdown
from .db import connect
from .evidence_repair_migration import (
    BACKUP_PATH,
    BACKUP_SHA256,
    EvidenceMigrationError,
    _overlay_rows,
    _write_bytes,
    _write_json_atomic,
)
from .record_refs import build_reference_index, resolve_reference
from .validator import validate_record_candidate


MIGRATION_ID = "20261001-step3-category5-pattern-references"

SUCCESSORS = {
    "patterns/2026-07-29-authority-response-authority-related-cues-trigger-a-predictable-response.md":
        "pattern.authority-response-authority-related-cues-trigger-a-predictable-response",
    "patterns/2026-07-29-avoidance-loop-the-narrative-repeatedly-returns-to-avoidance-or-deferral-under-p.md":
        "pattern.avoidance-loop-the-narrative-repeatedly-returns-to-avoidance-or-deferral-under-p",
    "patterns/2026-07-29-decision-loop-the-narrative-repeatedly-circles-the-same-decision-without-closure.md":
        "pattern.decision-loop-the-narrative-repeatedly-circles-the-same-decision-without-closure",
    "patterns/2026-07-29-relational-loop-relational-dynamics-recur-as-a-stable-loop.md":
        "pattern.relational-loop-relational-dynamics-recur-as-a-stable-loop",
    "patterns/2026-07-29-work-loop-work-related-concerns-recur-across-records.md":
        "pattern.work-loop-work-related-concerns-recur-across-records",
    "reviews/2026-07-29-pattern-authority-response-authority-related-cues-trigger-a-predictable-response.md":
        "skeptical_review.pattern-authority-response-authority-related-cues-trigger-a-predictable-response",
    "reviews/2026-07-29-pattern-avoidance-loop-the-narrative-repeatedly-returns-to-avoidance-or-deferral.md":
        "skeptical_review.pattern-avoidance-loop-the-narrative-repeatedly-returns-to-avoidance-or-deferral",
    "reviews/2026-07-29-pattern-decision-loop-the-narrative-repeatedly-circles-the-same-decision-without.md":
        "skeptical_review.pattern-decision-loop-the-narrative-repeatedly-circles-the-same-decision-without",
    "reviews/2026-07-29-pattern-relational-loop-relational-dynamics-recur-as-a-stable-loop-deterministic.md":
        "skeptical_review.pattern-relational-loop-relational-dynamics-recur-as-a-stable-loop-deterministic",
    "reviews/2026-07-29-pattern-work-loop-work-related-concerns-recur-across-records-deterministic-skept.md":
        "skeptical_review.pattern-work-loop-work-related-concerns-recur-across-records-deterministic-skept",
}


def prepare(vault: Path, db_path: Path, migration_root: Path) -> dict[str, Any]:
    metadata_path = migration_root / "migration.json"
    if metadata_path.exists():
        raise EvidenceMigrationError(f"migration is already prepared: {metadata_path}")
    migration_root.mkdir(parents=True, exist_ok=True)
    _verify_declared_successors(vault)

    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT record_path, record_id, content_sha256
            FROM record_quarantine
            WHERE active=1 AND record_type='pattern'
            ORDER BY record_path
            """
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 5:
        raise EvidenceMigrationError(f"expected 5 quarantined patterns, found {len(rows)}")

    records: list[dict[str, Any]] = []
    diff: list[str] = []
    mapped_total = 0
    self_version_total = 0
    canonical_total = 0
    reference_index = build_reference_index(vault)
    for row in rows:
        relative = str(row["record_path"])
        path = vault / relative
        before = path.read_bytes()
        if _sha(before) != str(row["content_sha256"]):
            raise EvidenceMigrationError(f"quarantine hash drift: {relative}")
        document = load_markdown(path)
        frontmatter = json.loads(json.dumps(document.frontmatter))
        record_id = str(frontmatter.get("id") or "")
        provenance = list(frontmatter.get("reference_provenance") or [])
        mapped = 0
        canonicalized = 0
        self_versions: set[str] = set()
        self_fields = 0
        for field_name in ("links", "supporting_records"):
            values = frontmatter.get(field_name)
            if not isinstance(values, list):
                continue
            repaired: list[str] = []
            for value in values:
                successor = SUCCESSORS.get(value) if isinstance(value, str) else None
                if not successor:
                    resolution = resolve_reference(value, vault, reference_index)
                    if resolution.ok:
                        target = resolution.target
                    elif resolution.repairable and resolution.kind != "merged":
                        target = resolution.target
                        canonicalized += 1
                    else:
                        raise EvidenceMigrationError(
                            f"unapproved or ambiguous reference in {relative}:{field_name}: "
                            f"{value!r} ({resolution.kind}: {resolution.detail})"
                        )
                    if target not in repaired:
                        repaired.append(target)
                    continue
                if successor == record_id and value.startswith("patterns/"):
                    self_versions.add(value)
                    self_fields += 1
                    continue
                if successor not in repaired:
                    repaired.append(successor)
                mapped += 1
            frontmatter[field_name] = repaired

        for old_target in sorted(self_versions):
            archive_path = "archive/patterns/superseded-" + Path(old_target).name
            provenance.append(
                {
                    "field": ["links", "supporting_records"],
                    "original_value": old_target,
                    "status": "historical_predecessor_version",
                    "archive_path": archive_path,
                    "shared_stable_id": record_id,
                    "reason": "Stable-id replacement would create a semantically meaningless self-edge.",
                    "evidence": "Archived predecessor declares this live record in superseded_by_path.",
                    "migration": MIGRATION_ID,
                }
            )
        if len(self_versions) != 1 or self_fields != 2:
            raise EvidenceMigrationError(
                f"expected one predecessor duplicated across two fields in {relative}; "
                f"found {len(self_versions)} predecessor(s), {self_fields} field instance(s)"
            )
        frontmatter["reference_provenance"] = provenance
        frontmatter["reference_repair"] = {
            "migration": MIGRATION_ID,
            "policy": "declared successor only; historical self-version preserved as provenance",
            "reviewed_by": "owner",
        }
        rendered = dump_markdown(frontmatter, document.body)
        candidate = validate_record_candidate(
            path, frontmatter, document.body, vault=vault, reference_index=reference_index
        )
        errors = [issue.message for issue in candidate.issues if issue.severity == "error"]
        if errors:
            raise EvidenceMigrationError(f"candidate remains invalid: {relative}: {errors}")
        after = rendered.encode("utf-8")
        _write_bytes(migration_root / "before" / relative, before, 0o600)
        _write_bytes(migration_root / "after" / relative, after, 0o600)
        diff.extend(
            difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
        records.append(
            {
                "action": "repair",
                "path": relative,
                "record_id": record_id,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "before_sha256": _sha(before),
                "after_sha256": _sha(after),
                "mapped_field_instances": mapped,
                "self_version_field_instances": self_fields,
                "canonicalized_field_instances": canonicalized,
            }
        )
        mapped_total += mapped
        self_version_total += self_fields
        canonical_total += canonicalized

    if mapped_total != 70 or self_version_total != 10:
        raise EvidenceMigrationError(
            f"reviewed count mismatch: mapped={mapped_total}, self-version={self_version_total}"
        )
    if canonical_total != 914:
        raise EvidenceMigrationError(
            f"exact path/alias/basename count mismatch: expected 914, found {canonical_total}"
        )
    paths = [str(row["path"]) for row in records]
    prior = _overlay_rows(db_path, paths)
    if len(prior) != 5 or any(int(row["active"]) != 1 for row in prior.values()):
        raise EvidenceMigrationError("all five patterns must be actively quarantined")
    (migration_root / "applied.diff").write_text("".join(diff), encoding="utf-8")
    os.chmod(migration_root / "applied.diff", 0o600)
    preview = migration_root / "mapping-preview.md"
    if preview.exists():
        shutil.copy2(preview, migration_root / "owner-approved-mapping.md")
    metadata = {
        "format": 1,
        "migration_id": MIGRATION_ID,
        "state": "prepared",
        "prepared_at": _now(),
        "vault": str(vault.resolve()),
        "database": str(db_path.resolve()),
        "restore_anchor": {"path": BACKUP_PATH, "sha256": BACKUP_SHA256, "predates_migration": True},
        "owner_approval": "Approved as proposed, including the self-version exception.",
        "mapped_field_instances": mapped_total,
        "self_version_field_instances": self_version_total,
        "canonicalized_field_instances": canonical_total,
        "records": records,
        "prior_quarantine_rows": prior,
        "released": [],
        "failed": [],
    }
    _write_json_atomic(metadata_path, metadata)
    return metadata


def _verify_declared_successors(vault: Path) -> None:
    for old_target, successor in SUCCESSORS.items():
        kind = old_target.split("/", 1)[0]
        archived = vault / "archive" / kind / f"superseded-{Path(old_target).name}"
        if not archived.is_file():
            raise EvidenceMigrationError(f"declared predecessor is missing: {archived}")
        old = load_markdown(archived)
        live_rel = str(old.frontmatter.get("superseded_by_path") or "")
        live = vault / live_rel
        if not live.is_file():
            raise EvidenceMigrationError(f"declared successor is missing: {live_rel}")
        current = load_markdown(live)
        if old.frontmatter.get("id") != successor or current.frontmatter.get("id") != successor:
            raise EvidenceMigrationError(f"stable-id mismatch for {old_target}")
        if old.frontmatter.get("summary") != current.frontmatter.get("summary"):
            raise EvidenceMigrationError(f"semantic summary mismatch for {old_target}")
        if kind == "reviews" and old.body != current.body:
            raise EvidenceMigrationError(f"review body mismatch for {old_target}")
        if kind == "patterns":
            for key in ("pattern_type", "hypothesis"):
                if old.frontmatter.get(key) != current.frontmatter.get(key):
                    raise EvidenceMigrationError(f"pattern semantics mismatch for {old_target}: {key}")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
