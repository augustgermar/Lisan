"""Fail-closed boundary for structured records entering a Lisan vault.

Markdown remains the durable source of truth, while SQLite supplies a short
transaction around id allocation.  The file replacement itself is atomic;
the reservation is committed first so concurrent writers cannot both pass a
filesystem existence check and mint the same live id.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..paths import sqlite_path
from .db import connect
from .record_refs import INDEXED_DIRECTORIES
from .validator import ValidationIssue, validate_record_candidate


class RecordWriteRejected(ValueError):
    """A structured record failed validation or id allocation."""

    def __init__(self, path: Path, issues: list[ValidationIssue] | list[str]):
        self.path = Path(path)
        self.issues = [issue.message if isinstance(issue, ValidationIssue) else str(issue) for issue in issues]
        joined = "; ".join(self.issues)
        super().__init__(f"Record write rejected for {self.path}: {joined}")


def infer_vault_for_record(path: Path) -> Path | None:
    """Return the vault root when *path* is in a structured top-level area."""
    absolute = Path(path).expanduser().resolve(strict=False)
    parts = absolute.parts
    for index, part in enumerate(parts):
        if part in INDEXED_DIRECTORIES and index > 0:
            return Path(*parts[:index])
    return None


def _error_messages(report) -> Counter[str]:
    return Counter(issue.message for issue in report.issues if issue.severity == "error")


def _new_errors(path: Path, vault: Path, frontmatter: dict[str, Any], body: str) -> list[str]:
    proposed = validate_record_candidate(path, frontmatter, body, vault=vault)
    proposed_errors = _error_messages(proposed)
    if not path.exists():
        return list(proposed_errors.elements())

    # Legacy records are repaired category by category.  An update may retain
    # an existing defect, but it may never add one; otherwise the boundary
    # would make incremental cleanup impossible and encourage raw-file bypass.
    from ..frontmatter import FrontmatterError, load_markdown

    try:
        current = load_markdown(path)
    except (FrontmatterError, OSError):
        return list(proposed_errors.elements())
    existing = validate_record_candidate(
        path, current.frontmatter, current.body, vault=vault
    )
    remaining = proposed_errors - _error_messages(existing)
    return list(remaining.elements())


_RESERVATION_SQL = """
CREATE TABLE IF NOT EXISTS record_id_reservations (
    vault_path TEXT NOT NULL,
    record_id TEXT NOT NULL,
    record_path TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'committed')),
    token TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (vault_path, record_id),
    UNIQUE (vault_path, record_path)
)
"""


@dataclass(slots=True)
class _Reservation:
    db_path: Path
    vault_key: str
    record_id: str
    record_path: str
    token: str
    prior_rows: list[tuple[str, str, str, str, str, str]]


def _reserve_id(path: Path, vault: Path, record_id: str, db_path: Path) -> _Reservation | None:
    rel = path.relative_to(vault).as_posix()
    if "archive" in Path(rel).parts:
        return None
    vault_key = str(vault.resolve())
    token = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    conn = connect(db_path)
    prior_rows: list[tuple[str, str, str, str, str, str]] = []
    try:
        conn.execute(_RESERVATION_SQL)
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT vault_path, record_id, record_path, state, token, updated_at
            FROM record_id_reservations
            WHERE vault_path = ? AND (record_id = ? OR record_path = ?)
            """,
            (vault_key, record_id, rel),
        ).fetchall()
        prior_rows = [tuple(str(value) for value in row) for row in rows]
        for row in rows:
            if row[1] == record_id and row[2] != rel:
                raise RecordWriteRejected(path, [f"Duplicate id {record_id} reserved by {row[2]}"])

        # The materialized index predates this reservation ledger.  Consult it
        # too, so the first post-upgrade write cannot collide with a legacy id.
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
        ).fetchone()
        if table:
            indexed = conn.execute("SELECT path FROM files WHERE id = ?", (record_id,)).fetchone()
            if indexed and str(indexed[0]) != rel:
                raise RecordWriteRejected(
                    path, [f"Duplicate id {record_id} already indexed at {indexed[0]}"]
                )

        conn.execute(
            "DELETE FROM record_id_reservations WHERE vault_path = ? AND record_path = ?",
            (vault_key, rel),
        )
        conn.execute(
            """
            INSERT INTO record_id_reservations
                (vault_path, record_id, record_path, state, token, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(vault_path, record_id) DO UPDATE SET
                record_path=excluded.record_path,
                state='pending',
                token=excluded.token,
                updated_at=excluded.updated_at
            """,
            (vault_key, record_id, rel, token, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _Reservation(db_path, vault_key, record_id, rel, token, prior_rows)


def _finish(reservation: _Reservation) -> None:
    conn = connect(reservation.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        updated = conn.execute(
            """
            UPDATE record_id_reservations
            SET state='committed', updated_at=?
            WHERE vault_path=? AND record_id=? AND token=?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                reservation.vault_key,
                reservation.record_id,
                reservation.token,
            ),
        ).rowcount
        if updated != 1:
            raise RuntimeError("record id reservation disappeared before commit")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release(reservation: _Reservation) -> None:
    conn = connect(reservation.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM record_id_reservations WHERE vault_path=? AND record_id=? AND token=?",
            (reservation.vault_key, reservation.record_id, reservation.token),
        )
        for row in reservation.prior_rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO record_id_reservations
                    (vault_path, record_id, record_path, state, token, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _atomic_replace(path: Path, rendered: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prior_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, prior_mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_if_structured_record(
    path: Path,
    frontmatter: dict[str, Any],
    body: str,
    rendered: str,
    *,
    db_path: Path | None = None,
) -> bool:
    """Validate and atomically write a record; return False for non-records."""
    vault = infer_vault_for_record(path)
    if vault is None:
        return False
    # macOS exposes /tmp as a symlink to /private/tmp.  Normalize both sides
    # before relative-path and duplicate checks or a perfectly in-vault write
    # is misclassified as external.
    path = Path(path).expanduser().resolve(strict=False)
    issues = _new_errors(path, vault, frontmatter, body)
    if issues:
        raise RecordWriteRejected(path, issues)
    record_id = str(frontmatter.get("id", "")).strip()
    if not record_id:
        # The validator should already have produced this, but fail closed if
        # its wording changes rather than creating an unreserved record.
        raise RecordWriteRejected(path, ["Missing required frontmatter field: id"])
    target_db = Path(db_path) if db_path else sqlite_path()
    reservation = _reserve_id(path, vault, record_id, target_db)
    existed = path.exists()
    prior_bytes = path.read_bytes() if existed else None
    try:
        _atomic_replace(path, rendered)
        if reservation is not None:
            _finish(reservation)
    except Exception:
        # SQLite and the filesystem cannot share a native transaction.  Make
        # the second phase compensating: if finalizing the id ledger fails,
        # restore the exact prior bytes (or remove the just-created file)
        # before releasing the reservation.
        if existed and prior_bytes is not None:
            _atomic_replace(path, prior_bytes.decode("utf-8"))
        elif path.exists():
            path.unlink()
        if reservation is not None:
            _release(reservation)
        raise
    return True
