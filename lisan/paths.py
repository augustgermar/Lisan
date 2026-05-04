from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_vault_root() -> Path:
    env_value = os.environ.get("LISAN_VAULT")
    if env_value:
        return Path(env_value).expanduser()
    return repo_root() / "lisan-vault"


def vault_root(base: Path | None = None) -> Path:
    if base is not None:
        return base / "lisan-vault"
    return default_vault_root()


def config_path(base: Path | None = None) -> Path:
    return (base or repo_root()) / "config.yaml"


def sqlite_path(base: Path | None = None) -> Path:
    return (base or repo_root()) / "lisan.sqlite"


def embeddings_path(base: Path | None = None) -> Path:
    return (base or repo_root()) / "embeddings.bin"


def schemas_dir(base: Path | None = None) -> Path:
    return (base or repo_root()) / "lisan" / "schemas"


def ensure_repo_layout(base: Path | None = None) -> None:
    root = base or repo_root()
    vault = vault_root(base)
    for rel in [
        "primer",
        "state",
        "entities/people",
        "entities/places",
        "entities/projects",
        "entities/organizations",
        "episodes",
        "knowledge/frameworks",
        "knowledge/legal",
        "knowledge/financial",
        "knowledge/technical",
        "evidence/artifacts",
        "evidence/records",
        "evidence/corrections",
        "decisions",
        "open_loops",
        "contradictions",
        "transcripts",
        "manifests",
        "arenas",
        "archive/episodes",
        "archive/entities",
        "archive/open_loops",
        "drafts",
        "reports",
    ]:
        (vault / rel).mkdir(parents=True, exist_ok=True)
    for rel in [
        "backups",
        "prompts",
        "lisan/schemas",
        ".githooks",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)
