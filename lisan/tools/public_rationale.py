"""Structural redaction gate for publishing self-repair proposals upstream.

See docs/self_repair_upstream_workorder.md (WO-REPAIR-UPSTREAM). Two
independent passes run before anything leaves the machine:

1. ``build_public_rationale`` assembles a PR title/body from structural
   fields only — a deviation class, a list of already-validated patch
   paths, and a bare record count — through a closed set of fixed
   templates. It is never given a loop's summary/description, its links,
   any entity id, or any vault path content.
2. ``assert_no_vault_leakage`` does not trust pass 1 (or any other caller)
   to have gotten it right. It scans the assembled text against a denylist
   built live from the index — every entity alias on file, every indexed
   record path, plus email/phone regex classes — and refuses outright on
   the first match. Fail closed, no partial redaction.

Both passes are index-only for v1 (docs/self_repair_upstream_workorder.md
§3.3): they read what ``rebuild_index.py`` already indexes, never the
vault's raw file content. Raw-content scanning is a documented future
hardening pass, not a change to this module's contract.

This module is itself listed in ``self_repair.py``'s ``PROTECTED_BASENAMES``
— the repair loop may never modify the mechanism that decides what it is
permitted to say about itself.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..paths import sqlite_path
from .db import connect as _db_connect
from .self_repair import SelfRepairRefused

# One fixed, parameterized template per deviation class deviations.py can
# currently emit (docs/self_repair_upstream_workorder.md §2.3). Keep this in
# sync with deviations.py's seven classes; a class added there without a
# matching entry here is not an error — it degrades to _GENERIC_TEMPLATE
# instead of refusing (decided 2026-11-09, §3.4).
_CLASS_TEMPLATES: dict[str, str] = {
    "cross_kind": (
        "The deterministic deviation scanner found a cross-kind record "
        "collision (class `cross_kind`): two records of different kinds "
        "referred to the same underlying thing without being reconciled."
    ),
    "dangling": (
        "The deterministic deviation scanner found a dangling reference "
        "(class `dangling`): a record pointed at another record that no "
        "longer resolves."
    ),
    "thin": (
        "The deterministic deviation scanner found an under-specified "
        "record (class `thin`): required structural fields were missing "
        "or left as placeholders."
    ),
    "stale": (
        "The deterministic deviation scanner found a stale record (class "
        "`stale`): a record's review date passed without being revisited."
    ),
    "interocept": (
        "The deterministic deviation scanner found an interoception "
        "consistency issue (class `interocept`): the agent's self-reported "
        "state diverged from what its own records show."
    ),
    "capture_defect": (
        "The deterministic deviation scanner found a capture-pipeline "
        "defect (class `capture_defect`): a capture produced a record that "
        "failed its own pipeline's invariants."
    ),
    "skill_health": (
        "The deterministic deviation scanner found a skill-health issue "
        "(class `skill_health`): a registered skill failed its own health "
        "check."
    ),
}

_GENERIC_TEMPLATE = (
    "The deterministic deviation scanner found a deviation of class "
    "`{klass}`. This install's public_rationale.py has no class-specific "
    "template for `{klass}` yet, so this description was built from the "
    "generic structural fallback (docs/self_repair_upstream_workorder.md "
    "§3.4): only the deviation class, a record count, and the changed file "
    "paths are described here, the same closed field set every other "
    "template uses."
)

# Conservative on purpose: separators are required, so a bare run of digits
# (a hash, a patch line count) never trips this class by accident.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)\d{3}[\s.-]\d{3}[\s.-]\d{4}(?!\d)")

# Denylist terms shorter than this match almost anything and would turn the
# gate into a standing refusal; drop them rather than let them wedge every
# future publish.
_MIN_DENYLIST_TERM_LEN = 3


@dataclass(frozen=True)
class PublicRationale:
    title: str
    body: str
    deviation_class: str
    used_generic_template: bool


def build_public_rationale(
    *,
    deviation_class: str,
    changed_paths: list[str],
    record_count: int = 1,
    install_id: str = "unknown",
) -> PublicRationale:
    """Structural-fields-only PR title/body for one verified proposal.

    Never pass this function a loop's summary/description, its links, any
    entity id, or vault path content — only the deviation class, the
    already-validated patch paths, and a bare count.
    """
    klass = str(deviation_class or "").strip()
    if not klass:
        raise SelfRepairRefused("public rationale requires a deviation class")
    paths = sorted({str(p) for p in (changed_paths or []) if p})
    if not paths:
        raise SelfRepairRefused("public rationale requires at least one changed file path")

    template = _CLASS_TEMPLATES.get(klass)
    used_generic = template is None
    text = template if template is not None else _GENERIC_TEMPLATE.format(klass=klass)

    file_list = "\n".join(f"- `{p}`" for p in paths)
    plural = "s" if len(paths) != 1 else ""
    title = f"self-repair: {klass} ({len(paths)} file{plural})"
    body = (
        f"{text}\n\n"
        f"**Records affected:** {int(record_count)}\n\n"
        f"**Changed files:**\n{file_list}\n\n"
        "**Verification:** this proposal passed an independent full test "
        "suite plus a targeted probe before becoming eligible for "
        "publication (examiner ≠ examinee — the resident self-repair "
        "Phase A contract).\n\n"
        f"Install: install-{install_id} (anonymous, not reversible to any identity)\n"
    )
    return PublicRationale(title=title, body=body, deviation_class=klass, used_generic_template=used_generic)


def _index_denylist(db_path: Path | None) -> set[str]:
    """Every alias/canonical name on file, plus every indexed record path.

    Index-only (docs/self_repair_upstream_workorder.md §3.3): this reads
    the same tables rebuild_index.py already maintains, never the vault's
    raw file content.
    """
    conn = _db_connect(db_path or sqlite_path())
    conn.row_factory = sqlite3.Row
    try:
        terms: set[str] = set()
        try:
            for row in conn.execute("SELECT DISTINCT alias FROM entity_aliases"):
                value = str(row["alias"] or "").strip()
                if len(value) >= _MIN_DENYLIST_TERM_LEN:
                    terms.add(value)
        except sqlite3.OperationalError:
            pass
        try:
            for row in conn.execute("SELECT DISTINCT path FROM files"):
                value = str(row["path"] or "").strip()
                if value:
                    terms.add(value)
        except sqlite3.OperationalError:
            pass
        return terms
    finally:
        conn.close()


def assert_no_vault_leakage(text: str, *, db_path: Path | None = None) -> None:
    """Independent check: does not trust ``build_public_rationale`` (or any
    other caller) to have redacted correctly. Raises ``SelfRepairRefused``
    on the first match; never publishes a partially-redacted remainder."""
    if _EMAIL_RE.search(text):
        raise SelfRepairRefused("public rationale refused: looks like an email address")
    if _PHONE_RE.search(text):
        raise SelfRepairRefused("public rationale refused: looks like a phone number")
    lowered = text.lower()
    for term in _index_denylist(db_path):
        if term.lower() in lowered:
            raise SelfRepairRefused(f"public rationale refused: matched a vault-index term ({term!r})")
