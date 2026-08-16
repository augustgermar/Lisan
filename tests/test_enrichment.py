from __future__ import annotations

import tempfile
from pathlib import Path

from lisan.frontmatter import dump_markdown, load_markdown, write_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.enrichment import EnrichmentResolution, _append_source_log, seek
from lisan.tools.record_factory import new_entity
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.transcript_lane import search_transcripts
from lisan.providers.embeddings import IndexEmbedding, QueryEmbedding


def _loop(vault: Path, loop_id: str = "open_loop.thin-ruth") -> Path:
    path = vault / "open_loops" / "thin-ruth.md"
    write_markdown(
        path,
        {
            "id": loop_id,
            "type": "open_loop",
            "created": "2026-08-15",
            "updated": "2026-08-15",
            "status": "active",
            "significance": "medium",
            "domain_primary": "relational",
            "domain_secondary": [],
            "privacy": "personal",
            "disclosure": "private",
            "summary": "Ruth Varga is thin in my model",
            "links": [],
            "confidence": "high",
            "confidence_basis": "test",
            "last_confirmed": "2026-08-15",
            "review_after": "2026-08-15",
            "priority": "medium",
            "owner": "agent",
            "next_action": "",
            "blocked_by": [],
        },
        "# Thin person\n",
    )
    return path


def _env() -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    ensure_repo_layout(root)
    vault = vault_root(root)
    entity = new_entity(vault, "Ruth Varga", subtype="person", summary="Ruth Varga is a person.").path
    return tmp, root, vault, entity


def _config() -> dict:
    return {
        "drive": {"action_tier": 3},
        "enrichment": {"max_candidates_per_source": 5},
        "retrieval": {"embeddings": {"mode": "hash", "hash_dimensions": 32}},
    }


class _SemanticTranscriptEmbeddings:
    settings = {"provider": "test", "model": "test-semantic", "mode": "semantic", "hash_dimensions": 2}

    def embed_records(self, texts: list[str]) -> IndexEmbedding:
        vectors = [[1.0, 0.0] if "mother" in text.lower() else [0.0, 1.0] for text in texts]
        return IndexEmbedding(vectors, "semantic", "test-semantic", 2, True)

    def embed_query(self, text: str) -> QueryEmbedding:
        vector = [1.0, 0.0] if "relationship" in text.lower() else [0.0, 1.0]
        return QueryEmbedding(vector, "semantic", 2, True)


def test_current_transcript_wins_and_closes_loop():
    tmp, root, vault, entity = _env()
    try:
        loop = _loop(vault)
        current = vault / "transcripts" / "current.md"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text(
            "## Conversation — 10:00 [chat]\n\nUSER: Ruth Varga is Dana's mother.\n",
            encoding="utf-8",
        )
        result = seek(
            vault=vault, db_path=root / "lisan.sqlite", loop_id="open_loop.thin-ruth",
            deficit_id="ruth.relationship", deficit="Ruth Varga relationship to Dana",
            entity_path=entity, config=_config(), current_transcript=current,
        )
        assert result["status"] == "resolved"
        assert result["ring"] == "transcript"
        entity_fm = load_markdown(entity).frontmatter
        assert entity_fm["source_log"][0]["source_uri"].startswith(str(current))
        assert load_markdown(loop).frontmatter["status"] == "resolved"
    finally:
        tmp.cleanup()


def test_historical_transcript_is_used_after_current_misses():
    tmp, root, vault, entity = _env()
    try:
        _loop(vault)
        current = vault / "transcripts" / "current.md"
        historical = vault / "transcripts" / "2026-08-01.md"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("## Conversation — 10:00 [chat]\n\nUSER: unrelated note.\n", encoding="utf-8")
        historical.write_text(
            "## Conversation — 09:00 [old]\n\nUSER: Ruth Varga is Dana's mother.\n",
            encoding="utf-8",
        )
        result = seek(
            vault=vault, db_path=root / "lisan.sqlite", loop_id="open_loop.thin-ruth",
            deficit_id="ruth.relationship", deficit="Ruth Varga relationship to Dana",
            entity_path=entity, config=_config(), current_transcript=current,
            historical_transcripts=[historical],
        )
        assert result["status"] == "resolved"
        assert result["ring"] == "transcript"
        assert str(historical) in load_markdown(entity).frontmatter["source_log"][0]["source_uri"]
    finally:
        tmp.cleanup()


def test_historical_transcripts_are_discovered_and_embedded(tmp_path: Path):
    vault = vault_root(tmp_path)
    entity = new_entity(vault, "Ruth Varga", subtype="person", summary="Ruth Varga is a person.").path
    loop = _loop(vault)
    historical = vault / "transcripts" / "2026-07-01.md"
    historical.parent.mkdir(parents=True, exist_ok=True)
    historical.write_text(
        "## Conversation — 09:00 [old]\n\nUSER: Ruth Varga is Dana's mother.\n",
        encoding="utf-8",
    )

    result = seek(
        vault=vault, db_path=tmp_path / "lisan.sqlite", loop_id="open_loop.thin-ruth",
        deficit_id="ruth.relationship", deficit="Ruth Varga relationship to Dana",
        entity_path=entity, config=_config(), embedding_provider=_SemanticTranscriptEmbeddings(),
    )

    assert result["status"] == "resolved"
    assert result["ring"] == "transcript"
    assert str(historical) in load_markdown(entity).frontmatter["source_log"][0]["source_uri"]
    sidecar = tmp_path / "transcript_embeddings.bin"
    assert sidecar.exists()
    sidecar_text = sidecar.read_text(encoding="utf-8")
    assert '"content_hash"' in sidecar_text
    assert '"embedding_hash"' in sidecar_text
    assert load_markdown(loop).frontmatter["status"] == "resolved"


def test_transcript_embedding_can_rank_a_paraphrase(tmp_path: Path):
    vault = vault_root(tmp_path)
    transcript = vault / "transcripts" / "2026-07-01.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "## Conversation — 09:00 [old]\n\nUSER: Ruth Varga is Dana's mother.\n"
        "\n## Conversation — 10:00 [other]\n\nUSER: The weather was pleasant today.\n",
        encoding="utf-8",
    )

    hits = search_transcripts(
        "Ruth Varga relationship to Dana", vault=vault, db_path=tmp_path / "lisan.sqlite",
        embedding_provider=_SemanticTranscriptEmbeddings(), limit=1,
    )

    assert len(hits) == 1
    assert "mother" in hits[0].excerpt.lower()
    assert hits[0].embedding_score > 0.9
    assert hits[0].embedding_hash


def test_vault_is_the_fallback_after_transcripts():
    tmp, root, vault, entity = _env()
    try:
        _loop(vault)
        current = vault / "transcripts" / "current.md"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("## Conversation — 10:00 [chat]\n\nUSER: unrelated note.\n", encoding="utf-8")
        episode = vault / "episodes" / "2026-08-10-ruth.md"
        write_markdown(
            episode,
            {"id": "episode.ruth", "type": "episode", "created": "2026-08-10", "updated": "2026-08-10", "summary": "Ruth Varga relationship"},
            "Ruth Varga is Dana's mother.\n",
        )
        db = root / "lisan.sqlite"
        rebuild_index(vault, db_path=db, embeddings_file=root / "embeddings.bin")
        result = seek(
            vault=vault, db_path=db, loop_id="open_loop.thin-ruth",
            deficit_id="ruth.relationship", deficit="Ruth Varga relationship to Dana",
            entity_path=entity, config=_config(), current_transcript=current,
        )
        assert result["status"] == "resolved"
        assert result["ring"] == "vault"
    finally:
        tmp.cleanup()


def test_owner_question_is_informed_when_no_source_resolves():
    tmp, root, vault, entity = _env()
    try:
        loop = _loop(vault)
        result = seek(
            vault=vault, db_path=root / "lisan.sqlite", loop_id="open_loop.thin-ruth",
            deficit_id="ruth.relationship", deficit="Ruth Varga relationship to Dana",
            entity_path=entity, config=_config(),
        )
        assert result["status"] == "needs_owner"
        owner_question = load_markdown(loop).frontmatter["owner_question"]
        assert "Ruth Varga" in owner_question
        from lisan.tools.drive import phrase_question

        assert phrase_question(load_markdown(loop).frontmatter) == owner_question
    finally:
        tmp.cleanup()


def test_provenance_failure_quarantines_resolution():
    tmp, root, vault, entity = _env()
    try:
        _loop(vault)
        current = vault / "transcripts" / "current.md"
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_text("## Conversation — 10:00 [chat]\n\nUSER: Ruth Varga is Dana's mother.\n", encoding="utf-8")

        def fail(_resolution):
            raise OSError("simulated provenance lock")

        result = seek(
            vault=vault, db_path=root / "lisan.sqlite", loop_id="open_loop.thin-ruth",
            deficit_id="ruth.relationship", deficit="Ruth Varga relationship to Dana",
            entity_path=entity, config=_config(), current_transcript=current,
            provenance_writer=fail,
        )
        assert result["status"] == "pending"
        assert not load_markdown(entity).frontmatter.get("source_log")
        pending = Path(result["pending_path"])
        assert load_markdown(pending).frontmatter["status"] == "quarantined"
    finally:
        tmp.cleanup()


def test_direct_evidence_supersedes_inference_and_caps_inference():
    tmp, root, vault, entity = _env()
    try:
        doc = load_markdown(entity)
        fm = dict(doc.frontmatter)
        fm["source_log"] = [{
            "date": "2026-08-14", "text": "Ruth may be Dana's mother.",
            "basis": "inference", "confidence": 0.9, "claim_key": "ruth.relationship",
        }]
        write_markdown(entity, fm, doc.body)
        _append_source_log(
            entity,
            EnrichmentResolution(
                text="Ruth Varga is Dana's mother.", source_type="transcript",
                source_uri="transcripts/2026-08-15.md#chat", claim_key="ruth.relationship",
            ),
            vault=vault, db_path=root / "lisan.sqlite",
        )
        entries = load_markdown(entity).frontmatter["source_log"]
        assert entries[0]["status"] == "superseded"
        assert entries[1]["basis"] == "direct_evidence"
        assert entries[0]["confidence"] == 0.9  # existing data is not rewritten; it is superseded
        _append_source_log(
            entity,
            EnrichmentResolution(
                text="Ruth might also be Dana's guardian.", source_type="vault",
                source_uri="episodes/ruth.md", basis="inference", confidence=0.9,
                claim_key="ruth.guardian",
            ),
            vault=vault, db_path=root / "lisan.sqlite",
        )
        assert load_markdown(entity).frontmatter["source_log"][-1]["confidence"] == 0.6
    finally:
        tmp.cleanup()
