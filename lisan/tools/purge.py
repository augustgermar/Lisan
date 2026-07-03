from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import save_default_config
from ..paths import embeddings_path, ensure_repo_layout, repo_root, sqlite_path, vault_root, write_seed_files
from .backup import create_backup


@dataclass(slots=True)
class PurgeResult:
    base: Path
    vault: Path
    removed_paths: list[str] = field(default_factory=list)
    seeded_files: list[str] = field(default_factory=list)
    backup_created: bool = False
    backup_archive_path: str | None = None
    config_reset: bool = False


def purge_installation(
    base: Path | None = None,
    *,
    preserve_config: bool = False,
    backup_before: bool = False,
    backup_destination: Path | None = None,
) -> PurgeResult:
    base = base or repo_root()
    vault = vault_root(base)
    result = PurgeResult(base=base, vault=vault)

    if backup_before:
        backup_destination = backup_destination or (base.parent / f"{base.name}-purge-backups")
        backup = create_backup(vault=vault, destination=backup_destination)
        result.backup_created = True
        result.backup_archive_path = str(backup.archive_path)

    for path in _paths_to_remove(base=base, vault=vault):
        if preserve_config and path.name in ("config.json", "config.yaml"):
            continue
        if path.exists():
            _remove_path(path)
            result.removed_paths.append(str(path))

    ensure_repo_layout(base)
    result.seeded_files = write_seed_files(vault)
    if not preserve_config:
        save_default_config(base / "config.json")
        result.config_reset = True
    return result


def _paths_to_remove(*, base: Path, vault: Path) -> list[Path]:
    return [
        vault,
        base / "backups",
        sqlite_path(base),
        embeddings_path(base),
        base / "config.json",
        base / "config.yaml",
    ]


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
