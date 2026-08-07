"""Gates on the things a stranger's first five minutes depend on.

Every check here failed, or could have failed, on a clean checkout while the
developer's own install stayed green. That asymmetry is the point: the suite
runs almost exclusively on a machine where Lisan is already configured, so the
fresh-install path is the least-exercised code in the project and the most
consequential — it is the only part every new user runs.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from lisan.paths import config_path, seed_config_file


def _repo() -> Path:
    return Path(__file__).resolve().parents[1]


def _strip_comments(node):
    """config.example.json documents itself with ``__comment`` keys."""
    if isinstance(node, dict):
        return {k: _strip_comments(v) for k, v in node.items() if not k.startswith("__comment")}
    return node


def _key_paths(node, prefix: str = "") -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(prefix + key)
            found |= _key_paths(value, prefix + key + ".")
    return found


# ── version ──────────────────────────────────────────────────────────────────

def test_version_is_the_same_in_both_places_it_is_written():
    """``lisan.__version__`` and the pyproject version are maintained by hand
    in two files, and one of them is what the agent reports about itself.

    ``self_model`` stamps ``__version__`` into the generated capability
    manifest — the instrument built so questions about the system's own state
    are answered from live truth rather than memory. An instrument that has
    drifted from the installed artifact is a confabulation with a citation,
    which is worse than not having one.
    """
    from lisan import __version__

    pyproject = (_repo() / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert match, "pyproject.toml has no version line"
    assert match.group(1) == __version__, (
        f"pyproject.toml says {match.group(1)}, lisan/__init__.py says {__version__}"
    )


# ── the shipped example config ───────────────────────────────────────────────

def test_example_config_is_valid_json():
    json.loads((_repo() / "config.example.json").read_text(encoding="utf-8"))


def test_example_config_documents_every_default_the_code_carries():
    """The example is what a new owner edits; DEFAULT_CONFIG is what the code
    falls back to. Nothing kept them in step, and they had drifted — the
    example never mentioned ``jobs.drain_on_capture``, so the only way to
    discover a real runtime default was to read config.py.

    Extra keys in the example are fine and expected: it documents blocks
    (``drive``, ``scheduler``, ``identity.ceremony``) that the code defaults
    handle inline. The asymmetry is deliberate — a documented setting the code
    ignores is harmless, an active default nobody can see is not.
    """
    from lisan.config import DEFAULT_CONFIG

    example = _strip_comments(json.loads((_repo() / "config.example.json").read_text(encoding="utf-8")))
    missing = _key_paths(DEFAULT_CONFIG) - _key_paths(example)
    assert not missing, (
        "config.example.json is missing keys that DEFAULT_CONFIG defines, so a new "
        f"owner cannot see them: {sorted(missing)}"
    )


def test_example_config_ships_the_execution_layer_off():
    """The Adjutant acts on the world. It ships off, and the file a stranger
    copies is the last place that can quietly stop being true."""
    example = json.loads((_repo() / "config.example.json").read_text(encoding="utf-8"))
    adjutant = example.get("adjutant", {})
    assert adjutant.get("enabled") is False
    assert adjutant.get("calibration") is False


# ── seeding the live config ──────────────────────────────────────────────────

def test_seed_config_writes_config_json_when_absent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "config.example.json").write_text('{"providers": {}}', encoding="utf-8")
        written = seed_config_file(root)
        assert written == root / "config.json"
        assert json.loads(written.read_text(encoding="utf-8")) == {"providers": {}}


def test_seed_config_never_overwrites_an_existing_config():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "config.example.json").write_text('{"providers": {}}', encoding="utf-8")
        (root / "config.json").write_text('{"mine": true}', encoding="utf-8")
        assert seed_config_file(root) is None
        assert json.loads((root / "config.json").read_text(encoding="utf-8")) == {"mine": True}


def test_seed_config_leaves_a_legacy_yaml_install_alone():
    """config_path() prefers config.json the moment one exists. Seeding beside
    a legacy config.yaml would therefore not add a config — it would replace
    the one the install actually runs on, silently, with the shipped example.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "config.example.json").write_text('{"providers": {}}', encoding="utf-8")
        (root / "config.yaml").write_text('{"legacy": true}', encoding="utf-8")
        assert seed_config_file(root) is None
        assert not (root / "config.json").exists()
        assert config_path(root) == root / "config.yaml"


def test_seed_config_survives_a_missing_example():
    with tempfile.TemporaryDirectory() as tmp:
        assert seed_config_file(Path(tmp)) is None
