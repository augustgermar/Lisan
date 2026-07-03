from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class UninstallResult:
    install_root: Path
    vault: Path | None
    removed_paths: list[str] = field(default_factory=list)
    removed_path_entries: list[str] = field(default_factory=list)
    kept_vault: bool = True
    vault_removed: bool = False


def uninstall_installation(
    install_root: Path | None = None,
    *,
    bin_dir: Path | None = None,
    keep_vault: bool = True,
) -> UninstallResult:
    install_root = install_root or default_install_root()
    bin_dir = bin_dir or default_bin_dir()
    vault = install_root / "vault"
    result = UninstallResult(install_root=install_root, vault=vault)

    for path in _paths_to_remove(install_root, vault=vault, keep_vault=keep_vault, bin_dir=bin_dir):
        if path.exists():
            _remove_path(path)
            result.removed_paths.append(str(path))

    if not keep_vault:
        result.kept_vault = False
        result.vault_removed = True

    rc_file = _rc_file()
    if rc_file and rc_file.exists():
        removed = _remove_path_entry(rc_file, bin_dir)
        if removed:
            result.removed_path_entries.append(str(rc_file))

    _cleanup_empty_parent_dirs(install_root)
    return result


def default_install_root() -> Path:
    env_value = os.environ.get("LISAN_HOME")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".lisan"


def default_bin_dir() -> Path:
    env_value = os.environ.get("LISAN_BIN_DIR")
    if env_value:
        return Path(env_value).expanduser()
    return Path.home() / ".local" / "bin"


def _paths_to_remove(
    install_root: Path,
    *,
    vault: Path,
    keep_vault: bool,
    bin_dir: Path,
) -> list[Path]:
    paths = [
        install_root / "repo",
        install_root / "venv",
        install_root / "config.json",
        install_root / "config.yaml",
        install_root / "lisan.sqlite",
        install_root / "embeddings.bin",
        bin_dir / "lisan",
    ]
    if not keep_vault:
        paths.append(install_root / "backups")
        paths.append(vault)
    return paths


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _rc_file() -> Path | None:
    shell = Path(os.environ.get("SHELL", "")).name
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        bashrc = home / ".bashrc"
        if bashrc.exists():
            return bashrc
        return home / ".bash_profile"
    return None


def _remove_path_entry(rc_file: Path, bin_dir: Path) -> bool:
    try:
        original = rc_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False

    lines = original.splitlines()
    updated: list[str] = []
    removed = False
    skip_next_marker = False
    target = str(bin_dir)
    marker = "# Added by Lisan installer"

    for line in lines:
        if skip_next_marker:
            if line.strip() == "" or line.startswith("export PATH="):
                removed = True
                skip_next_marker = False
                continue
            skip_next_marker = False

        if marker in line:
            removed = True
            skip_next_marker = True
            continue

        if target in line and line.startswith("export PATH="):
            removed = True
            continue

        updated.append(line)

    if removed:
        rc_file.write_text("\n".join(updated).rstrip() + ("\n" if updated else ""), encoding="utf-8")
    return removed


def _cleanup_empty_parent_dirs(install_root: Path) -> None:
    try:
        if install_root.exists() and not any(install_root.iterdir()):
            install_root.rmdir()
    except OSError:
        pass
