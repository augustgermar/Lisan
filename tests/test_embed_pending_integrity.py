"""A row says "embedded" only once its vector is on disk, and concurrent
drains cannot erase each other's vectors."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from lisan.paths import ensure_vault_layout
from lisan.tools import rebuild_index as ri
from lisan.tools import vector_store
from lisan.tools.record_factory import new_entity


class _FakeProvider:
    settings = {"batch_size": 64}

    def __init__(self, *_):
        pass

    def embed_records(self, texts):
        return SimpleNamespace(mode_used="semantic", dimension=2, model="fake",
                               vectors=[[1.0, float(len(t) % 7)] for t in texts])


def _pending_vault(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    db = tmp_path / "index.sqlite"
    new_entity(vault, "Robin Vale")
    new_entity(vault, "Sam Ortiz")
    conn = sqlite3.connect(db)
    conn.close()
    ri.rebuild_index(vault, db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE files SET embedding_status = 'pending'")
    conn.commit()
    conn.close()
    return vault, db


def _statuses(db):
    conn = sqlite3.connect(db)
    try:
        return {row[0] for row in conn.execute("SELECT embedding_status FROM files")}
    finally:
        conn.close()


def test_a_failed_vector_write_leaves_rows_pending(tmp_path, monkeypatch):
    vault, db = _pending_vault(tmp_path)
    monkeypatch.setattr(ri, "EmbeddingProvider", _FakeProvider)

    def broken_write(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ri, "write_embeddings", broken_write, raising=False)
    monkeypatch.setattr(vector_store, "write_embeddings", broken_write)
    with pytest.raises(OSError):
        ri.embed_pending_records(vault, db)
    assert _statuses(db) == {"pending"}


def test_drain_writes_vectors_then_marks_rows(tmp_path, monkeypatch):
    vault, db = _pending_vault(tmp_path)
    monkeypatch.setattr(ri, "EmbeddingProvider", _FakeProvider)
    out = ri.embed_pending_records(vault, db)
    assert out["embedded"] >= 2
    assert _statuses(db) == {"embedded"}
    index = vector_store.load_index(db.parent / "embeddings.bin")
    assert len(index.vectors) == out["embedded"]
    assert not list(db.parent.glob("embeddings.bin.*.tmp"))


def test_the_drain_holds_the_embeddings_lock(tmp_path, monkeypatch):
    import fcntl

    vault, db = _pending_vault(tmp_path)
    seen = []

    class Probe(_FakeProvider):
        def embed_records(self, texts):
            with open(db.parent / "embeddings.bin.lock", "a+") as other:
                try:
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen.append("free")
                    fcntl.flock(other.fileno(), fcntl.LOCK_UN)
                except OSError:
                    seen.append("held")
            return super().embed_records(texts)

    monkeypatch.setattr(ri, "EmbeddingProvider", Probe)
    ri.embed_pending_records(vault, db)
    assert seen and set(seen) == {"held"}
