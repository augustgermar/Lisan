from __future__ import annotations

import os
import sys
import tempfile
import warnings
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _looks_like_a_test_process() -> bool:
    """Is this process running a test suite, whatever the runner?

    Asks what the process *is* rather than how it was started — the same shape
    as ``_notify_owner`` asking whose vault is escalating instead of whether it
    was launched under systemd. ``tests/__init__.py`` sets ``LISAN_DATA_HOME``
    for containment, but it is only imported when the suite loads as a package:
    ``python -m unittest discover -s tests`` puts ``tests/`` on sys.path and
    imports each module top-level, so the init never runs. Measured 2026-07-29
    — under that runner ``LISAN_DATA_HOME`` was unset and ``data_root()``
    resolved to the live install, which is the exact resolution that replaced
    971 production vectors with an empty stub.

    Neither framework is imported anywhere in lisan's runtime, so their presence
    in ``sys.modules`` is a sound signal rather than a guess.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules or "unittest" in sys.modules


# Resolved once per process so every ambient caller in a test run agrees, and
# so the warning is emitted once rather than per call.
_TEST_DATA_ROOT: Path | None = None


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


def data_root() -> Path:
    """Where MUTABLE state lives — the index and the embedding store.

    Deliberately separate from :func:`repo_root`, which also locates read-only
    package resources (prompts, schemas). Both defaulted to the install
    directory, so any process that resolved a data path ambiently wrote into
    the developer's live install: on 2026-07-27 a test run silently replaced a
    populated production ``embeddings.bin`` with an empty stub, and the same
    ambient ``sqlite_path`` had already been logged as a deviation candidate.

    ``LISAN_DATA_HOME`` redirects only the mutable paths, so a test run (or any
    sandboxed process) can be pointed somewhere harmless without breaking
    prompt and schema loading.

    A test process never gets the live install back. That containment used to
    depend on ``tests/__init__.py`` being imported before anything else, which
    one real runner skips (see :func:`_looks_like_a_test_process`); deciding it
    here makes it hold for every runner, including one nobody has written yet.
    Set ``LISAN_DATA_HOME`` to choose the location, or
    ``LISAN_ALLOW_TEST_DATA_ROOT=1`` to opt out deliberately — one test asserts
    the production default and must still be able to.
    """
    env_value = os.environ.get("LISAN_DATA_HOME")
    if env_value:
        root = Path(env_value).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        return root
    if _looks_like_a_test_process() and os.environ.get("LISAN_ALLOW_TEST_DATA_ROOT") != "1":
        global _TEST_DATA_ROOT
        if _TEST_DATA_ROOT is None:
            _TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="lisan-test-data-"))
            # Audible, not silent: a redirect nobody is told about is the same
            # failure mode as an embedder that skips and reports success.
            warnings.warn(
                "lisan.paths.data_root(): a test process resolved the mutable data root "
                f"ambiently; redirected to {_TEST_DATA_ROOT} rather than the live install "
                f"at {repo_root()}. Pass an explicit path, or set LISAN_DATA_HOME.",
                RuntimeWarning,
                stacklevel=2,
            )
        _TEST_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        return _TEST_DATA_ROOT
    return repo_root()


def sqlite_path(base: Path | None = None) -> Path:
    return (base or data_root()) / "lisan.sqlite"


def embeddings_path(base: Path | None = None) -> Path:
    return (base or data_root()) / "embeddings.bin"


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
    # Commander's intent has generated frontmatter (dates, version), so its
    # template lives with the intent tooling; imported lazily to keep paths
    # free of tool dependencies at import time.
    from .tools.intent import default_intent_document

    seeds[vault / "primer" / "intent.md"] = default_intent_document()
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
        "schedules",
        "confirmations",
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
