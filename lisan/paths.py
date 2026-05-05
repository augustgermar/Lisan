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


_IDENTITY_TEMPLATE = "# Identity\n"

_OPERATING_STYLE_TEMPLATE = "# Operating Style\n"

_ARENAS_TEMPLATE = """\
# Arenas Definition

> Stable infrastructure. Changes to this file require a migration log entry in arena-migration-log.md.

## Internal Arenas

| # | Arena | Core Question |
|---|-------|---------------|
| 1 | **Physical** | Is the user's body and mind supporting the life they want to live? |
| 2 | **Environmental** | Does the user's environment make them more capable, calm, and effective? |
| 3 | **Financial** | Is the user gaining financial power, resilience, and optionality? |
| 4 | **Relational** | Are the user's relationships nourishing, honest, and aligned? |
| 5 | **Work** | Is the user producing useful work that increases income, leverage, skill, or optionality? |

## External Arenas

| # | Arena | Core Question |
|---|-------|---------------|
| 6 | **Status** | Does the user appear credible, competent, respectable, and socially legible? |
| 7 | **Appearance** | Does the user visually present as attractive, healthy, competent, and intentional? |
| 8 | **Competence** | Do others experience the user as capable, reliable, intelligent, and effective? |
| 9 | **Social Presence** | Does the user's presence make people want more contact, trust, and cooperation? |
| 10 | **Desirability** | Does the user present as someone others can desire, respect, and feel emotionally safe with? |
"""

_BACKUP_LOG_TEMPLATE = """\
# Backup Log

## Policy

| Tier | Scope | Frequency | Method |
|------|-------|-----------|--------|
| 0 | Working vault | Continuous | Git (local) |
| 1 | Local encrypted backup | Daily | age encryption → local disk |
| 2 | Offline encrypted backup | Weekly | Encrypted → external SSD |
| 3 | Disaster recovery export | Monthly | Full vault + indices → offline storage |

**Restore test:** Monthly. Restore vault into temp directory, run rebuild-index, run validate.
Log result below.

## Run Log

<!-- Backup runs are appended here automatically by `lisan backup create`. -->
"""


def write_seed_files(vault: Path) -> list[str]:
    """Write starter content files that must exist but cannot be generated. Returns list of files written."""
    written = []
    seeds = {
        vault / "primer" / "identity.md": _IDENTITY_TEMPLATE,
        vault / "primer" / "operating-style.md": _OPERATING_STYLE_TEMPLATE,
        vault / "arenas" / "arenas-definition.md": _ARENAS_TEMPLATE,
        vault / "backup.md": _BACKUP_LOG_TEMPLATE,
    }
    for path, content in seeds.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            written.append(str(path.relative_to(vault.parent)))
    return written


def ensure_repo_layout(base: Path | None = None) -> None:
    root = base or repo_root()
    vault = vault_root(base)
    for rel in [
        "primer",
        "state",
        "entities/people",
        "entities/places",
        "entities/things",
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
        "transcripts/narrative",
        "manifests",
        "arenas",
        "archive/episodes",
        "archive/entities",
        "archive/open_loops",
        "drafts",
        "reports",
        "logs",
    ]:
        (vault / rel).mkdir(parents=True, exist_ok=True)
    for rel in [
        "backups",
        "prompts",
        "lisan/schemas",
        ".githooks",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)
