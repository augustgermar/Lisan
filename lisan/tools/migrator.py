from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..paths import vault_root


@dataclass(slots=True)
class MigrationPlan:
    needs_migration: bool
    actions: list[str]


def scan_vault_for_migrations(vault: Path | None = None) -> MigrationPlan:
    vault = vault or vault_root()
    actions: list[str] = []
    if not (vault / "primer").exists():
        actions.append("Create primer directory")
    if not (vault / "manifests").exists():
        actions.append("Create manifests directory")
    if not (vault / "transcripts").exists():
        actions.append("Create transcripts directory")
    return MigrationPlan(needs_migration=bool(actions), actions=actions)


def run_migration(vault: Path | None = None, dry_run: bool = True) -> MigrationPlan:
    plan = scan_vault_for_migrations(vault)
    if dry_run:
        return plan
    # The current scaffold only reports missing structural pieces.
    # Real migrations will be implemented once a structure change is needed.
    return plan

