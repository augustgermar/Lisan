from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..config import load_config
from ..paths import embeddings_path, repo_root, sqlite_path, vault_root
from ..utils import today_iso
from .manifest_gen import generate_manifests
from .rebuild_index import rebuild_index
from .validator import format_report, validate_vault


@dataclass(slots=True)
class BackupResult:
    archive_path: Path
    encrypted: bool
    restore_tested: bool
    restore_ok: bool
    restore_message: str


def create_backup(
    vault: Path | None = None,
    destination: Path | None = None,
    recipient: str | None = None,
    identity: str | None = None,
    encrypt: bool = False,
) -> BackupResult:
    vault = vault or vault_root()
    config = load_config()
    backup_cfg = config.get("backup", {}) if isinstance(config, dict) else {}
    destination = destination or (repo_root() / str(backup_cfg.get("destination_dir", "backups")))
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = destination / f"lisan-backup-{stamp}.tar.gz"
    tar_path = archive_path
    encrypted = False
    encrypt = encrypt or bool(backup_cfg.get("encrypt_by_default", False))
    recipient = recipient or os.environ.get(str(backup_cfg.get("recipient_env", "LISAN_BACKUP_RECIPIENT")), "")
    identity = identity or os.environ.get(str(backup_cfg.get("identity_env", "LISAN_BACKUP_IDENTITY")), "")
    age_env = str(backup_cfg.get("age_binary_env", "AGE_BIN"))
    if encrypt and not recipient:
        raise ValueError("Backup encryption requested but no recipient configured")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_tar = Path(tmpdir) / archive_path.name
        _write_tarball(tmp_tar, vault)
        if encrypt and recipient:
            age = shutil.which(os.environ.get(age_env, "age"))
            if age:
                encrypted = True
                tar_path = archive_path.with_suffix(archive_path.suffix + ".age")
                _encrypt_with_age(age, tmp_tar, tar_path, recipient)
            else:
                shutil.copy2(tmp_tar, archive_path)
        else:
            shutil.copy2(tmp_tar, archive_path)

    restore_tested = False
    restore_ok = False
    restore_message = "restore test not run"
    return BackupResult(
        archive_path=tar_path,
        encrypted=encrypted,
        restore_tested=restore_tested,
        restore_ok=restore_ok,
        restore_message=restore_message,
    )


def test_backup(
    archive_path: Path,
    vault: Path | None = None,
    identity: str | None = None,
) -> BackupResult:
    vault = vault or vault_root()
    config = load_config()
    backup_cfg = config.get("backup", {}) if isinstance(config, dict) else {}
    identity = identity or os.environ.get(str(backup_cfg.get("identity_env", "LISAN_BACKUP_IDENTITY")), "")
    if not archive_path.exists():
        raise FileNotFoundError(archive_path)

    restore_ok = False
    restore_message = "restore failed"
    with tempfile.TemporaryDirectory(prefix="lisan-restore-") as tmpdir:
        restore_root = Path(tmpdir)
        try:
            _restore_archive(archive_path, restore_root, identity=identity)
            restored_vault = restore_root / "lisan-vault"
            if not restored_vault.exists():
                raise FileNotFoundError(restored_vault)
            generate_manifests(restored_vault, write=True)
            report = validate_vault(restored_vault)
            rebuild_index(restored_vault, db_path=restore_root / "lisan.sqlite", embeddings_file=restore_root / "embeddings.bin")
            restore_ok = report.ok
            restore_message = "restore ok" if report.ok else format_report(report)
        except Exception as exc:
            restore_message = str(exc)
            restore_ok = False

    result = BackupResult(
        archive_path=archive_path,
        encrypted=archive_path.suffix.endswith(".age"),
        restore_tested=True,
        restore_ok=restore_ok,
        restore_message=restore_message,
    )
    _write_backup_log(vault, result)
    return result


def latest_backup_path(destination: Path | None = None) -> Path | None:
    destination = destination or (repo_root() / "backups")
    if not destination.exists():
        return None
    candidates = sorted(destination.glob("lisan-backup-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def backup_status(vault: Path | None = None, destination: Path | None = None) -> str:
    vault = vault or vault_root()
    config = load_config()
    backup_cfg = config.get("backup", {}) if isinstance(config, dict) else {}
    destination = destination or (repo_root() / str(backup_cfg.get("destination_dir", "backups")))
    latest = latest_backup_path(destination)
    lines = ["# Backup Status", ""]
    if latest:
        lines.append(f"latest_archive: {latest}")
        lines.append(f"modified: {datetime.fromtimestamp(latest.stat().st_mtime).isoformat(timespec='seconds')}")
    else:
        lines.append("latest_archive: none")
    backup_log = vault / "backup.md"
    if backup_log.exists():
        lines.append("")
        lines.append("## Backup Log")
        lines.append(backup_log.read_text(encoding="utf-8").strip())
    return "\n".join(lines).rstrip() + "\n"


def write_backup_log(vault: Path | None, result: BackupResult) -> Path:
    vault = vault or vault_root()
    return _write_backup_log(vault, result)


def _write_tarball(path: Path, vault: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lisan-backup-stage-") as tmpdir:
        stage_root = Path(tmpdir)
        if vault.exists():
            shutil.copytree(vault, stage_root / "lisan-vault", dirs_exist_ok=True)
        for rel in ["lisan.sqlite", "embeddings.bin", "config.yaml"]:
            source = repo_root() / rel
            if source.exists():
                shutil.copy2(source, stage_root / rel)
        with tarfile.open(path, "w:gz") as tar:
            for rel in ["lisan-vault", "lisan.sqlite", "embeddings.bin", "config.yaml"]:
                source = stage_root / rel
                if source.exists():
                    tar.add(source, arcname=rel)


def _encrypt_with_age(age_bin: str, source: Path, destination: Path, recipient: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([age_bin, "-r", recipient, "-o", str(destination), str(source)], check=True)


def _restore_archive(archive_path: Path, restore_root: Path, identity: str | None = None) -> None:
    if archive_path.suffix.endswith(".age"):
        age = shutil.which(os.environ.get("AGE_BIN", "age"))
        if not age:
            raise RuntimeError("age binary not found for encrypted restore")
        if not identity:
            raise RuntimeError("age identity required to restore encrypted backup")
        decrypted = restore_root / "restored.tar.gz"
        subprocess.run([age, "-d", "-i", identity, "-o", str(decrypted), str(archive_path)], check=True)
        archive_path = decrypted
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(restore_root)


def _write_backup_log(vault: Path, result: BackupResult) -> Path:
    path = vault / "backup.md"
    lines = [
        "# Backup Log",
        "",
        f"## {datetime.now().isoformat(timespec='seconds')}",
        "",
        f"- timestamp: {today_iso()}",
        f"- archive: {result.archive_path}",
        f"- encrypted: {str(result.encrypted).lower()}",
        f"- restore_tested: {str(result.restore_tested).lower()}",
        f"- restore_ok: {str(result.restore_ok).lower()}",
        f"- restore_message: {result.restore_message}",
        "",
    ]
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text((existing.rstrip() + "\n\n" if existing.strip() else "") + "\n".join(lines), encoding="utf-8")
    return path
