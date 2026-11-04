"""`lisan doctor` measures the system's own architectural claims."""
from __future__ import annotations

import json

from lisan.frontmatter import write_markdown
from lisan.paths import ensure_vault_layout
from lisan.tools.doctor import FAIL, PASS, WARN, exit_code, run_doctor
from tests.intent_helpers import adopt_intent


def _by_name(results):
    return {r.name: r for r in results}


def test_a_healthy_fresh_vault_has_no_failures(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    adopt_intent(vault)
    results = _by_name(run_doctor(vault, tmp_path / "missing.sqlite"))
    for name in ("authority", "jobs", "self-model", "intent", "self-repair"):
        assert results[name].status == PASS, results[name]
    assert exit_code(list(results.values())) == 0


def test_an_unadopted_intent_is_a_warning_not_silence(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    assert _by_name(run_doctor(vault, tmp_path / "x.sqlite"))["intent"].status == WARN


def test_polluted_story_logs_are_reported(tmp_path):
    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    write_markdown(vault / "entities" / "people" / "robin.md",
                   {"id": "entity.robin", "type": "entity",
                    "source_log": [{"text": '{"action": "full"} Skeptic approved'}, {"text": "Robin moved."}]}, "# Robin")
    check = _by_name(run_doctor(vault, tmp_path / "x.sqlite"))["story-logs"]
    assert check.status == WARN and "1 of 2" in check.evidence


def test_cli_exposes_the_doctor(tmp_path, capsys):
    from lisan.cli import main

    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    adopt_intent(vault)
    code = main(["doctor", "--vault", str(vault), "--db-path", str(tmp_path / "x.sqlite"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and {c["name"] for c in out} >= {"authority", "jobs", "edges"}
    assert all(c["status"] != FAIL for c in out)


def test_passage_level_vectors_count_as_embedded(tmp_path):
    import sqlite3

    from lisan.tools.vector_store import write_embeddings

    vault = tmp_path / "vault"
    ensure_vault_layout(vault)
    db = tmp_path / "x.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE files (id TEXT, embedding_status TEXT)")
    conn.executemany("INSERT INTO files VALUES (?, 'embedded')", [("rec.whole",), ("rec.split",)])
    conn.commit()
    conn.close()
    write_embeddings(tmp_path / "embeddings.bin", [
        ("rec.whole", [1.0, 0.0]),
        ("rec.split::passage:0:abcdef012345", [0.0, 1.0]),
        ("rec.split::passage:1:543210fedcba", [1.0, 1.0]),
    ], model="t", dimension=2)
    check = _by_name(run_doctor(vault, db))["embeddings"]
    assert check.status == PASS, check
