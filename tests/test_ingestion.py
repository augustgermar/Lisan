from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from lisan.cli import main
from lisan.frontmatter import load_markdown
from lisan.paths import ensure_repo_layout, vault_root
from lisan.tools.ingest import audit_ingestion, list_manifest, scan_path, show_artifact
from lisan.tools.ingest_batches import get_batch, quarantine_batch, summarize_batch
from lisan.tools.jobs import list_jobs, run_jobs_worker
from lisan.tools.rebuild_index import rebuild_index
from lisan.tools.retrieval import assemble_context
from lisan.tools.validator import validate_vault


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ensure_repo_layout(self.root)
        self.vault = vault_root(self.root)
        self.db_path = self.root / "lisan.sqlite"
        self.embeddings_path = self.root / "embeddings.bin"
        self.src = self.root / "source"
        self.src.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _artifact_docs(self) -> list[tuple[Path, dict[str, object]]]:
        docs: list[tuple[Path, dict[str, object]]] = []
        for path in sorted((self.vault / "evidence" / "artifacts").glob("*.md")):
            doc = load_markdown(path)
            docs.append((path, doc.frontmatter))
        return docs

    def _rebuild(self) -> None:
        rebuild_index(vault=self.vault, db_path=self.db_path, embeddings_file=self.embeddings_path)

    def test_scan_creates_artifacts_and_extracts_provenance(self) -> None:
        note = self.src / "note.md"
        note.write_text(
            "# Note\n\nAlex asked Jordan to share the update.\nJordan maybe thought the request was unusual.\n",
            encoding="utf-8",
        )
        data = self.src / "data.json"
        data.write_text('{"a": 1, "b": 2}', encoding="utf-8")

        scan = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        self.assertEqual(scan["discovered"], 2)
        self.assertEqual(len(scan["queued_jobs"]), 2)

        repeat = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        self.assertEqual(len(repeat["queued_jobs"]), 2)
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 2)

        worker = run_jobs_worker(vault=self.vault, db_path=self.db_path, worker_id="worker-ingest")
        self.assertEqual(worker["failure_count"], 0)
        self.assertEqual(worker["success_count"], 6)

        self._rebuild()
        report = validate_vault(self.vault)
        self.assertTrue(report.ok, report.summary())

        docs = self._artifact_docs()
        self.assertEqual(len(docs), 2)
        note_doc = next(frontmatter for _, frontmatter in docs if frontmatter["file_name"] == "note.md")
        note_artifact_id = str(note_doc["id"])
        note_show = show_artifact(note_artifact_id, vault=self.vault, db_path=self.db_path)
        self.assertIsNotNone(note_show)
        self.assertEqual(note_show["artifact"]["frontmatter"]["ingestion_status"], "evidence_extracted")
        self.assertTrue(note_show["artifact"]["frontmatter"]["linked_evidence"])

        evidence_docs = []
        claim_docs = []
        for path in sorted((self.vault / "evidence" / "records").glob("*.md")):
            doc = load_markdown(path)
            if doc.frontmatter.get("type") == "evidence":
                evidence_docs.append(doc.frontmatter)
        for path in sorted((self.vault / "claims").glob("*.md")):
            doc = load_markdown(path)
            if doc.frontmatter.get("type") == "claim":
                claim_docs.append(doc.frontmatter)
        self.assertTrue(any(str(fm.get("artifact_ref")) == note_artifact_id for fm in evidence_docs))
        self.assertTrue(any(str(fm.get("artifact_ref")) == note_artifact_id for fm in claim_docs))
        self.assertTrue(all(str(fm.get("status")) != "confirmed" for fm in claim_docs))

        context = assemble_context("Steve rollout plan management", vault=self.vault, db_path=self.db_path)
        self.assertIn("## Artifacts", context)
        self.assertIn(note_artifact_id, context)
        self.assertIn("## Evidence", context)
        self.assertIn("## Claims", context)

    def test_real_scan_creates_batch_and_propagates_batch_id(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nA batch-tracked note.\n", encoding="utf-8")

        scan = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        batch_id = str(scan["batch_id"])
        self.assertTrue(batch_id)
        batch = get_batch(batch_id, db_path=self.db_path)
        self.assertIsNotNone(batch)
        self.assertEqual(batch["status"], "completed")

        docs = self._artifact_docs()
        self.assertEqual(len(docs), 1)
        artifact_frontmatter = docs[0][1]
        self.assertEqual(str(artifact_frontmatter.get("batch_id")), batch_id)

        manifest_rows = list_manifest(db_path=self.db_path)
        self.assertTrue(manifest_rows)
        self.assertEqual(str(manifest_rows[0].get("batch_id")), batch_id)

        jobs = list_jobs(db_path=self.db_path)
        self.assertTrue(jobs)
        self.assertTrue(all(str(job.get("batch_id")) == batch_id for job in jobs))

        batch_summary = summarize_batch(batch_id, db_path=self.db_path)
        self.assertIsNotNone(batch_summary)
        self.assertEqual(batch_summary["summary"]["artifacts"], 1)
        self.assertEqual(batch_summary["summary"]["jobs"], len(jobs))

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["ingest", "batches", "--db-path", str(self.db_path)])
        self.assertEqual(exit_code, 0)
        self.assertIn(batch_id, stdout.getvalue())

    def test_dry_run_creates_no_persisted_batch_and_reports_proposed_summary(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nDry run only.\n", encoding="utf-8")

        plan = scan_path(self.src, vault=self.vault, db_path=self.db_path, dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["would_create_batch"])
        self.assertIn("proposed_batch_summary", plan)
        self.assertFalse(self.db_path.exists())
        self.assertEqual(list((self.vault / "evidence" / "artifacts").glob("*.md")), [])

    def test_unchanged_files_do_not_duplicate_and_changed_hash_reingests(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nInitial content.\n", encoding="utf-8")

        first = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        first_artifact_id = str(first["manifest_changes"][0]["artifact_id"])
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 1)

        run_jobs_worker(vault=self.vault, db_path=self.db_path, worker_id="worker-1")
        self._rebuild()
        self.assertEqual(len(list((self.vault / "evidence" / "artifacts").glob("*.md"))), 1)

        second = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        self.assertEqual(len(second["queued_jobs"]), 0)
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 0)

        note.write_text("# Note\n\nChanged content with a new observation.\n", encoding="utf-8")
        third = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        self.assertEqual(third["discovered"], 1)
        self.assertNotEqual(str(third["manifest_changes"][0]["artifact_id"]), first_artifact_id)
        self.assertEqual(len(third["queued_jobs"]), 1)
        run_jobs_worker(vault=self.vault, db_path=self.db_path, worker_id="worker-2")
        self._rebuild()
        self.assertEqual(len(list((self.vault / "evidence" / "artifacts").glob("*.md"))), 2)
        audit = audit_ingestion(vault=self.vault, db_path=self.db_path)
        self.assertFalse(audit["duplicate_hashes"])

    def test_secrets_are_skipped_and_images_create_artifacts_without_text_extraction(self) -> None:
        secret = self.src / "secret.env"
        secret.write_text("API_KEY=abc123\n", encoding="utf-8")
        image = self.src / "diagram.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"binary-data")

        scan = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        self.assertEqual(scan["skipped"], 2)
        self.assertEqual(len(scan["queued_jobs"]), 0)

        secret_artifacts = [frontmatter for _, frontmatter in self._artifact_docs() if frontmatter["file_name"] == "secret.env"]
        self.assertEqual(secret_artifacts, [])

        image_doc = next(frontmatter for _, frontmatter in self._artifact_docs() if frontmatter["file_name"] == "diagram.png")
        image_artifact_id = str(image_doc["id"])
        image_show = show_artifact(image_artifact_id, vault=self.vault, db_path=self.db_path)
        self.assertEqual(image_show["artifact"]["frontmatter"]["ingestion_status"], "skipped")
        self.assertEqual(image_show["artifact"]["frontmatter"]["source_type"], "image")
        self.assertEqual(image_show["artifact"]["frontmatter"]["parse_errors"], ["Unsupported file type for parsing"])
        self.assertEqual(len(list_jobs(status="queued", db_path=self.db_path)), 0)

    def test_dry_run_creates_no_writes_and_reports_planned_actions(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nAlex asked Jordan to share the update.\n", encoding="utf-8")
        secret = self.src / "secret.env"
        secret.write_text("API_KEY=abc123\n", encoding="utf-8")
        image = self.src / "diagram.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"binary-data")

        plan = scan_path(self.src, vault=self.vault, db_path=self.db_path, dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["summary"]["total_files_seen"], 3)
        self.assertEqual(plan["summary"]["would_parse"], 1)
        self.assertEqual(plan["summary"]["would_create_artifact_only"], 1)
        self.assertEqual(plan["summary"]["would_skip"], 1)
        self.assertEqual(plan["summary"]["skipped_secret_like_files"], 1)
        self.assertFalse(self.db_path.exists())
        self.assertEqual(list((self.vault / "evidence" / "artifacts").glob("*.md")), [])
        self.assertTrue(any(item["planned_action"] == "create_artifact_and_parse" for item in plan["files"]))
        self.assertTrue(any(item["planned_action"] == "create_artifact_only" for item in plan["files"]))
        self.assertTrue(any(item["skip_reason"] for item in plan["files"]))

    def test_dry_run_json_is_valid_and_includes_planned_actions(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nAlex asked Jordan to share the update.\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = main(["ingest", "scan", str(self.src), "--dry-run", "--json", "--vault", str(self.vault), "--db-path", str(self.db_path)])
        self.assertEqual(exit_code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertIn("files", payload)
        self.assertTrue(any(item["planned_action"] for item in payload["files"]))

    def test_review_mode_declines_without_writes(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nAlex asked Jordan to share the update.\n", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("builtins.input", return_value="n"), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["ingest", "scan", str(self.src), "--review", "--vault", str(self.vault), "--db-path", str(self.db_path)])
        self.assertEqual(exit_code, 0)
        self.assertIn("Ingest review declined", stdout.getvalue())
        self.assertFalse(self.db_path.exists())
        self.assertEqual(list((self.vault / "evidence" / "artifacts").glob("*.md")), [])

    def test_audit_reports_pending_failed_and_skipped_records(self) -> None:
        pending = self.src / "pending.md"
        pending.write_text("# Pending\n\nThis note will be removed before parsing.\n", encoding="utf-8")
        secret = self.src / "secret.env"
        secret.write_text("API_KEY=abc123\n", encoding="utf-8")

        scan = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        self.assertEqual(scan["discovered"], 1)
        audit_before = audit_ingestion(vault=self.vault, db_path=self.db_path)
        self.assertEqual(len(audit_before["artifacts_awaiting_extraction"]), 1)
        self.assertEqual(len(audit_before["skipped_files"]), 1)
        self.assertEqual(len(audit_before["queued_extraction_jobs"]), 1)

        pending.unlink()
        worker = run_jobs_worker(vault=self.vault, db_path=self.db_path, worker_id="worker-audit")
        self.assertGreaterEqual(worker["failure_count"], 1)
        audit_after = audit_ingestion(vault=self.vault, db_path=self.db_path)
        self.assertTrue(audit_after["failed_extraction_jobs"])
        self.assertTrue(list_jobs(status="failed", db_path=self.db_path))

    def test_changed_and_unchanged_known_file_classification(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nInitial content.\n", encoding="utf-8")
        scan_path(self.src, vault=self.vault, db_path=self.db_path)
        run_jobs_worker(vault=self.vault, db_path=self.db_path, worker_id="worker-seed")
        unchanged = scan_path(self.src, vault=self.vault, db_path=self.db_path, dry_run=True)
        self.assertEqual(unchanged["files"][0]["classification"], "unchanged")
        note.write_text("# Note\n\nChanged content.\n", encoding="utf-8")
        changed = scan_path(self.src, vault=self.vault, db_path=self.db_path, dry_run=True)
        self.assertEqual(changed["files"][0]["classification"], "changed")

    def test_include_and_exclude_extension_filters_work(self) -> None:
        (self.src / "note.md").write_text("# Note\n\nA note.\n", encoding="utf-8")
        (self.src / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.src / ".hidden.txt").write_text("hidden\n", encoding="utf-8")

        plan = scan_path(
            self.src,
            vault=self.vault,
            db_path=self.db_path,
            dry_run=True,
            include_ext=[".md", ".txt", ".csv"],
            exclude_ext=[".csv"],
        )
        paths = [item["source_path"] for item in plan["files"]]
        note_path = str((self.src / "note.md").resolve())
        csv_path = str((self.src / "data.csv").resolve())
        hidden_path = str((self.src / ".hidden.txt").resolve())
        self.assertIn(note_path, paths)
        self.assertIn(csv_path, paths)
        self.assertNotIn(hidden_path, paths)
        note_plan = next(item for item in plan["files"] if item["source_path"] == note_path)
        csv_plan = next(item for item in plan["files"] if item["source_path"] == csv_path)
        self.assertEqual(note_plan["planned_action"], "create_artifact_and_parse")
        self.assertEqual(csv_plan["skip_reason"], "Extension excluded")

    def test_quarantined_batch_hides_artifacts_and_blocks_queued_jobs(self) -> None:
        note = self.src / "note.md"
        note.write_text("# Note\n\nThis batch will be quarantined.\n", encoding="utf-8")

        scan = scan_path(self.src, vault=self.vault, db_path=self.db_path)
        batch_id = str(scan["batch_id"])
        self._rebuild()

        quarantined = quarantine_batch(batch_id, "bad import", vault=self.vault, db_path=self.db_path)
        self.assertIsNotNone(quarantined)
        self.assertEqual(get_batch(batch_id, db_path=self.db_path)["status"], "quarantined")
        self.assertEqual(list_jobs(status="queued", db_path=self.db_path), [])
        canceled_jobs = list_jobs(status="canceled", db_path=self.db_path)
        self.assertTrue(any(str(job.get("batch_id")) == batch_id for job in canceled_jobs))

        context = assemble_context("quarantined batch note", vault=self.vault, db_path=self.db_path)
        self.assertNotIn("note.md", context)
        self.assertNotIn(batch_id, context)


if __name__ == "__main__":
    unittest.main()
