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
    """The live config file: config.json (the content has always been JSON).

    Installs predating the rename keep working: when config.json does not
    exist but a legacy config.yaml does, the legacy path is used — reads and
    writes stay on the file the install actually has until it is renamed.
    """
    root = base or repo_root()
    primary = root / "config.json"
    legacy = root / "config.yaml"
    if not primary.exists() and legacy.exists():
        return legacy
    return primary


def sqlite_path(base: Path | None = None) -> Path:
    return (base or repo_root()) / "lisan.sqlite"


def embeddings_path(base: Path | None = None) -> Path:
    return (base or repo_root()) / "embeddings.bin"


def skills_root(base: Path | None = None) -> Path:
    env_value = os.environ.get("LISAN_SKILLS_DIR")
    if env_value:
        return Path(env_value).expanduser()
    if base is not None:
        return base / "skills"
    return Path.home() / ".local" / "share" / "Lisan" / "skills"


def schemas_dir(base: Path | None = None) -> Path:
    return (base or repo_root()) / "lisan" / "schemas"


_IDENTITY_TEMPLATE = """# identity

the principal has not shared a narrative yet.
"""

_OPERATING_STYLE_TEMPLATE = """\
---
{
  "emotion-naming": null,
  "directness": null,
  "opener-style": null,
  "summary-length": null
}
---

# Operating Style

> Structured preferences live in the frontmatter above. Set values to control
> fallback-path behavior:
>
> - `emotion-naming`: `false` to forbid Lisan from naming emotions
>   prematurely in fallback responses. `null` = no opinion (default).
> - `directness`: `true` if you prefer terse, direct phrasing.
> - `opener-style`: `"minimal"` to skip preamble and small talk.
> - `summary-length`: `"short"` for terse summaries.
>
> Free-text notes below are read by the LLM path. The parser also checks the
> body for common preference phrases as a legacy fallback.

## Communication Style

_Not yet filled in._

## Working Style

_Not yet filled in._
"""

_HIGH_STAKES_TEMPLATE = """\
# High-stakes terms — topics that matter enough to always get full processing.
# Customize this list for your life. Add terms that signal important life areas;
# remove ones that don't apply. This file is local to your vault and is not
# committed to the repo.
#
# Future: Lisan will learn these dynamically from your usage patterns —
# topics that repeatedly appear in high-significance turns will be suggested
# as additions. For now, edit manually.
terms: []
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


def write_high_stakes_seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HIGH_STAKES_TEMPLATE, encoding="utf-8")


def write_seed_files(vault: Path) -> list[str]:
    """Write starter content files that must exist but cannot be generated. Returns list of files written."""
    written = []
    seeds = {
        vault / "primer" / "identity.md": _IDENTITY_TEMPLATE,
        vault / "primer" / "operating-style.md": _OPERATING_STYLE_TEMPLATE,
        vault / "primer" / "high-stakes.yaml": _HIGH_STAKES_TEMPLATE,
        vault / "backup.md": _BACKUP_LOG_TEMPLATE,
    }
    for path, content in seeds.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            written.append(str(path.relative_to(vault.parent)))
    return written


def ensure_vault_layout(vault: Path) -> None:
    for rel in [
        "primer",
        "state",
        "entities/people",
        "entities/agents",
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
        "patterns",
        "predictions",
        "claims",
        "reviews",
        "decisions",
        "open_loops",
        "contradictions",
        "transcripts",
        "transcripts/narrative",
        "manifests",
        "archive/episodes",
        "archive/entities",
        "archive/open_loops",
        "drafts",
        "reports",
        "logs",
    ]:
        (vault / rel).mkdir(parents=True, exist_ok=True)


def ensure_root_layout(root: Path) -> None:
    for rel in [
        "backups",
        "prompts",
        "lisan/schemas",
        ".githooks",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)


def ensure_repo_layout(base: Path | None = None) -> None:
    root = base or repo_root()
    vault = vault_root(base)
    ensure_vault_layout(vault)
    ensure_root_layout(root)
