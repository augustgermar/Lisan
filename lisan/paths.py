from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def vault_root(base: Path | None = None) -> Path:
    return (base or repo_root()) / "lisan-vault"


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
    for rel in [
        "lisan-vault/primer",
        "lisan-vault/state",
        "lisan-vault/entities/people",
        "lisan-vault/entities/places",
        "lisan-vault/entities/projects",
        "lisan-vault/entities/organizations",
        "lisan-vault/episodes",
        "lisan-vault/knowledge/frameworks",
        "lisan-vault/knowledge/legal",
        "lisan-vault/knowledge/financial",
        "lisan-vault/knowledge/technical",
        "lisan-vault/evidence/artifacts",
        "lisan-vault/evidence/records",
        "lisan-vault/evidence/corrections",
        "lisan-vault/decisions",
        "lisan-vault/open_loops",
        "lisan-vault/contradictions",
        "lisan-vault/transcripts",
        "lisan-vault/manifests",
        "lisan-vault/arenas",
        "lisan-vault/archive/episodes",
        "lisan-vault/archive/entities",
        "lisan-vault/archive/open_loops",
        "lisan-vault/drafts",
        "lisan-vault/reports",
        "prompts",
        "lisan/schemas",
        ".githooks",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)
