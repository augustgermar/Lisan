from __future__ import annotations

import json
from pathlib import Path

from lisan.tools.deixis_repair import build_manifest, write_manifest


def test_manifest_supports_transcript_evidence_and_refuses_guess(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "entities" / "people").mkdir(parents=True)
    (vault / "transcripts").mkdir()
    (vault / "entities" / "people" / "a.md").write_text(
        "On {{principal}} 20, 2026, the event happened.\n"
        "The undated report was prepared in {{principal}} 2099.\n",
        encoding="utf-8",
    )
    (vault / "transcripts" / "2026-08-20.md").write_text(
        "LISAN: The event happened on August 20, 2026.\n",
        encoding="utf-8",
    )

    candidates = build_manifest(vault)

    assert len(candidates) == 2
    assert candidates[0].proposed_value == "August 20, 2026"
    assert candidates[0].evidence_kind == "original transcript"
    assert candidates[1].proposed_value is None
    assert candidates[1].status == "unresolved"


def test_manifest_is_dry_run_and_writes_every_occurrence(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "open_loops").mkdir(parents=True)
    (vault / "knowledge").mkdir()
    (vault / "transcripts").mkdir()
    record = vault / "open_loops" / "x.md"
    original = "Birthday: {{principal}} 6. Again: {{principal}} 6.\n"
    record.write_text(original, encoding="utf-8")
    (vault / "knowledge" / "source.md").write_text("Birthday: August 6\n", encoding="utf-8")
    (vault / "transcripts" / "2026-08-12.md").write_text("USER: Birthday: August 6\n", encoding="utf-8")
    json_path = tmp_path / "manifest.json"
    markdown_path = tmp_path / "manifest.md"

    count, supported, files = write_manifest(vault, json_path, markdown_path)

    assert (count, supported, files) == (2, 2, 1)
    assert record.read_text(encoding="utf-8") == original
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload["candidates"]) == 2
    assert "dry-run-only" in markdown_path.read_text(encoding="utf-8")
