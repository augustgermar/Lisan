"""WO-8: the wipe refuses everything that is not a marked clone."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_EVALS = Path(__file__).resolve().parents[1] / "evals"


def _load():
    spec = importlib.util.spec_from_file_location("wipe_test", _EVALS / "wipe_test.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["wipe_test"] = module
    spec.loader.exec_module(module)
    return module


wipe = _load()


def _decoy_vault(tmp_path: Path, marked: bool) -> Path:
    vault = tmp_path / "decoy"
    (vault / "primer").mkdir(parents=True)
    (vault / "primer" / "identity-core.md").write_text("---\n---\nkernel", encoding="utf-8")
    (vault / "episodes").mkdir()
    (vault / "episodes" / "e.md").write_text("x", encoding="utf-8")
    if marked:
        (vault / wipe.CLONE_MARKER).write_text("clone", encoding="utf-8")
    return vault


def test_refuses_unmarked_target(tmp_path):
    vault = _decoy_vault(tmp_path, marked=False)
    with pytest.raises(wipe.WipeRefused, match="marker"):
        wipe.wipe_memory_layers(vault)
    assert (vault / "episodes" / "e.md").exists()  # untouched


def test_refuses_missing_and_nonvault_targets(tmp_path):
    with pytest.raises(wipe.WipeRefused):
        wipe.wipe_memory_layers(tmp_path / "nope")
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / wipe.CLONE_MARKER).write_text("clone", encoding="utf-8")
    with pytest.raises(wipe.WipeRefused, match="kernel"):
        wipe.wipe_memory_layers(bare)


def test_refuses_live_vault_by_path(monkeypatch, tmp_path):
    vault = _decoy_vault(tmp_path, marked=True)
    monkeypatch.setattr(wipe, "LIVE_VAULT", vault)
    with pytest.raises(wipe.WipeRefused, match="LIVE"):
        wipe.wipe_memory_layers(vault)


def test_wipes_memory_keeps_kernel(tmp_path):
    vault = _decoy_vault(tmp_path, marked=True)
    manifest = wipe.wipe_memory_layers(vault)
    assert not (vault / "episodes").exists() or not any((vault / "episodes").iterdir())
    assert (vault / "primer" / "identity-core.md").exists()
    assert "episodes/" in manifest["removed"]


def test_clone_gets_marker(tmp_path):
    source = _decoy_vault(tmp_path, marked=False)
    clone = wipe.make_clone(source, tmp_path / "clone")
    assert (clone / wipe.CLONE_MARKER).exists()
    assert (clone / "episodes" / "e.md").exists()
    with pytest.raises(wipe.WipeRefused, match="exists"):
        wipe.make_clone(source, tmp_path / "clone")
