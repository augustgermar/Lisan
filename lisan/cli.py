from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import load_config, save_default_config
from .agents import AdviceAgent, AnalystAgent, AssemblerAgent, DreamerAgent, ElicitorAgent, InterlocutorAgent, ListenerAgent, RouterAgent, SkepticAgent, WriterAgent
from .paths import ensure_repo_layout, ensure_root_layout, ensure_vault_layout, repo_root, sqlite_path, vault_root, write_seed_files
from .providers.base import LisanLLM, ProviderError
from .prompts import list_prompts, load_prompt
from .tools.assembler import assemble_context
from .tools.chat import run_chat
from .tools.capture import capture_text
from .tools.current_brief import write_current_brief
from .tools.conversation_digest import write_conversation_digest
from .tools.dreamer_ops import audit_patterns, format_pattern_audit, run_dreamer_task
from .tools.draft_review import review_draft
from .tools.archive import archive_open_loop
from .tools.analyst_ops import run_analyst_scan
from .tools.jobs import audit_jobs, cancel_job, format_job_audit, format_job_list, get_job, list_jobs, reap_stuck_jobs, retry_job, run_jobs_worker
from .tools.backup import backup_status, create_backup, latest_backup_path, test_backup, write_backup_log
from .tools.batch_review import generate_batch_review, write_batch_review
from .tools.epochs import epoch_entity
from .tools.drafts import promote_draft_to_episode
from .tools.editor import edit_record
from .tools.health_report import generate_health_report
from .tools.log import log_error
from .tools.ingest import (
    audit_ingestion,
    format_ingest_audit,
    format_ingest_batch_audit,
    format_ingest_batch_summary,
    format_ingest_batches,
    format_ingest_plan,
    format_reference_ingest_plan,
    ingest_reference_sources,
    format_ingest_status,
    plan_scan_path,
    scan_path,
    show_artifact,
)
from .tools.ingest_batches import get_batch, list_batches, quarantine_batch, summarize_batch
from .tools.heuristic_gate import score_text
from .tools.confidence_decay import detect_decay_candidates
from .tools.manifest_gen import generate_manifests
from .tools.migrator import run_migration
from .tools.purge import purge_installation
from .tools.uninstall import default_bin_dir, default_install_root, uninstall_installation
from .tools.primer_audit import run_primer_audit
from .tools.provider_diagnostics import diagnose_provider
from .tools.record_factory import new_claim, new_decision, new_evidence, new_evidence_correction, new_entity, new_episode, new_knowledge, new_open_loop, new_state, upsert_state
from .tools.rebuild_index import open_index_connection, rebuild_index
from .tools.narrative_state import conversation_history, load_narrative_state, render_narrative_state, reset_narrative_state
from .tools.tracing import format_recent_turn_traces, format_turn_trace, list_recent_turn_traces, load_turn_trace
from .tools.transcripts import append_transcript
from .tools.validator import format_report, validate_vault


def _split_csv_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _bootstrap_runtime(vault: Path, *, ensure_schema: bool = False) -> None:
    ensure_root_layout(repo_root())
    ensure_vault_layout(vault)
    if not (repo_root() / "config.json").exists():
        save_default_config()
    write_seed_files(vault)
    if ensure_schema:
        conn = open_index_connection(sqlite_path())
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    parser = argparse.ArgumentParser(prog="lisan")
    parser.add_argument("--version", action="version", version=f"lisan {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the default vault layout and config")
    purge = subparsers.add_parser("purge", help="Delete personal vault data and reset to a fresh start")
    purge.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    purge.add_argument("--preserve-config", action="store_const", const=True, default=None, help="Keep config.json instead of resetting it")
    purge.add_argument("--preserve-kernel", action="store_true", help="Keep the identity kernel (primer/identity-core.md) — wipe the autobiography, not the self (the Memory Wipe Test)")
    purge.add_argument("--backup-before", action="store_const", const=True, default=None, help="Create a backup before deleting anything")
    purge.add_argument("--backup-destination", type=Path, default=None, help="Write the optional pre-purge backup to this directory")

    uninstall = subparsers.add_parser("uninstall", help="Remove the managed Lisan install and launcher")
    uninstall.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    uninstall.add_argument("--purge-vault", action="store_true", help="Also delete the vault directory")
    uninstall.add_argument("--home", type=Path, default=None, help="Override the managed install root")
    uninstall.add_argument("--bin-dir", type=Path, default=None, help="Override the launcher directory")

    chat = subparsers.add_parser("chat", help="Start an interactive chat session")
    chat.add_argument("--vault", type=Path, default=vault_root())
    chat.add_argument("--conversation-id", default=None)
    chat.add_argument("--provider", default=None)
    chat.add_argument("--model", default=None)
    chat.add_argument("--trace", action="store_true", help="Print a compact trace summary after each turn")

    state = subparsers.add_parser("state", help="Inspect or update state files")
    state_subparsers = state.add_subparsers(dest="state_command", required=True)
    state_update = state_subparsers.add_parser("update", help="Overwrite a current state file")
    state_update.add_argument("--vault", type=Path, default=vault_root())
    state_update.add_argument("state_category", choices=["physical", "environmental", "financial", "relational", "work", "status", "appearance", "competence", "social_presence", "desirability"])
    state_update.add_argument("summary")
    state_update.add_argument("--state-secondary", "--arena-secondary", dest="state_secondary", action="append", default=[])
    state_update.add_argument("--privacy", default="personal")
    state_update.add_argument("--confidence", default="low")
    state_update.add_argument("--confidence-basis", default="User-authored placeholder")
    state_update.add_argument("--source", action="append", default=[])
    state_update.add_argument("--review-after", default=None)
    state_update.add_argument("--ttl-days", type=int, default=None)

    show = subparsers.add_parser("show", help="Display a vault record")
    show.add_argument("--path", type=Path, required=True)

    validate = subparsers.add_parser("validate", help="Validate vault files")
    validate.add_argument("--vault", type=Path, default=vault_root())

    intent_cmd = subparsers.add_parser("intent", help="Commander's intent: the authority document for execution")
    intent_subparsers = intent_cmd.add_subparsers(dest="intent_command", required=True)
    intent_show = intent_subparsers.add_parser("show", help="Render current intent with version and validity")
    intent_show.add_argument("--vault", type=Path, default=vault_root())
    intent_init_cmd = intent_subparsers.add_parser("init", help="Create primer/intent.md from the template")
    intent_init_cmd.add_argument("--vault", type=Path, default=vault_root())
    intent_init_cmd.add_argument("--force", action="store_true", help="Overwrite an existing intent.md")
    intent_edit = intent_subparsers.add_parser("edit", help="Open in $EDITOR; snapshot prior version and bump on save")
    intent_edit.add_argument("--vault", type=Path, default=vault_root())
    intent_history_cmd = intent_subparsers.add_parser("history", help="List intent snapshots")
    intent_history_cmd.add_argument("--vault", type=Path, default=vault_root())
    intent_check = intent_subparsers.add_parser("check", help="Dry-run the delegation gate for an arena + capability")
    intent_check.add_argument("arena")
    intent_check.add_argument("capability")
    intent_check.add_argument("--vault", type=Path, default=vault_root())

    adjutant_cmd = subparsers.add_parser("adjutant", help="The execution layer: poll, gate, execute against intent")
    adjutant_subparsers = adjutant_cmd.add_subparsers(dest="adjutant_command", required=True)
    adjutant_run = adjutant_subparsers.add_parser("run", help="One cycle: poll -> gate -> log verdicts (dry-run until enabled)")
    adjutant_run.add_argument("--vault", type=Path, default=vault_root())
    adjutant_run.add_argument("--db-path", type=Path, default=None)
    adjutant_status_cmd = adjutant_subparsers.add_parser("status", help="Last cycle, halts, pending confirmations, blocked tasks")
    adjutant_status_cmd.add_argument("--vault", type=Path, default=vault_root())
    adjutant_status_cmd.add_argument("--db-path", type=Path, default=None)
    adjutant_log_cmd = adjutant_subparsers.add_parser("log", help="Tail the adjutant audit log")
    adjutant_log_cmd.add_argument("limit", nargs="?", type=int, default=20)
    adjutant_log_cmd.add_argument("--db-path", type=Path, default=None)

    confirm_cmd = subparsers.add_parser("confirm", help="Approve or deny pending Adjutant confirmations")
    confirm_subparsers = confirm_cmd.add_subparsers(dest="confirm_command", required=True)
    confirm_list = confirm_subparsers.add_parser("list", help="List pending confirmations")
    confirm_list.add_argument("--vault", type=Path, default=vault_root())
    confirm_list.add_argument("--db-path", type=Path, default=None)
    confirm_approve = confirm_subparsers.add_parser("approve", help="Approve a confirmation by id")
    confirm_approve.add_argument("id")
    confirm_approve.add_argument("--vault", type=Path, default=vault_root())
    confirm_approve.add_argument("--db-path", type=Path, default=None)
    confirm_deny = confirm_subparsers.add_parser("deny", help="Deny a confirmation by id")
    confirm_deny.add_argument("id")
    confirm_deny.add_argument("--vault", type=Path, default=vault_root())
    confirm_deny.add_argument("--db-path", type=Path, default=None)

    manifest = subparsers.add_parser("manifest", help="Generate derived manifests")
    manifest.add_argument("--vault", type=Path, default=vault_root())
    manifest.add_argument("--no-write", action="store_true")

    ingest = subparsers.add_parser("ingest", help="Discover and process local artifacts")
    ingest.add_argument("--vault", type=Path, default=vault_root())
    ingest.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest.add_argument("--reference", nargs="+", type=Path, default=None, help="Ingest reference document(s) as chunked knowledge records")
    ingest.add_argument("--link-entity", action="append", default=[], help="Pre-link all reference chunks to this entity id or name")
    ingest.add_argument("--replace", action="store_true", help="Replace existing chunks for the same source document")
    ingest.add_argument("--on-exists", choices=["abort", "replace", "merge"], default=None, help="Policy when a reference source already exists")
    ingest.add_argument("--plan", action="store_true", help="Preview reference ingestion without writing")
    ingest.add_argument("--json", action="store_true", help="Emit machine-readable JSON for reference ingestion")
    ingest_subparsers = ingest.add_subparsers(dest="ingest_command", required=False)
    ingest_scan = ingest_subparsers.add_parser("scan", help="Scan a file or directory for ingestible artifacts")
    ingest_scan.add_argument("--vault", type=Path, default=vault_root())
    ingest_scan.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_scan.add_argument("--dry-run", action="store_true", help="Preview what would be ingested without writing anything")
    ingest_scan.add_argument("--review", action="store_true", help="Preview first and ask for confirmation before writing")
    ingest_scan.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ingest_scan.add_argument("--include-ext", default=None, help="Comma-separated list of extensions to include")
    ingest_scan.add_argument("--exclude-ext", default=None, help="Comma-separated list of extensions to exclude")
    ingest_scan.add_argument("--max-size-mb", type=float, default=None, help="Maximum file size in megabytes for ingestion")
    ingest_scan.add_argument("--include-hidden", action="store_true", help="Include hidden files and directories")
    ingest_scan.add_argument("--allow-restricted", action="store_true", help="Allow restricted sensitivity files")
    ingest_scan.add_argument("--allow-high", action="store_true", help="Allow high sensitivity files")
    ingest_scan.add_argument("--allow-sealed", action="store_true", help="Dangerous: allow sealed files to be scanned")
    ingest_scan.add_argument("path", type=Path)
    ingest_status = ingest_subparsers.add_parser("status", help="Show ingest manifest status")
    ingest_status.add_argument("--vault", type=Path, default=vault_root())
    ingest_status.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_run = ingest_subparsers.add_parser("run", help="Run queued ingest jobs")
    ingest_run.add_argument("--vault", type=Path, default=vault_root())
    ingest_run.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_run.add_argument("--provider", default=None)
    ingest_run.add_argument("--model", default=None)
    ingest_run.add_argument("--worker-id", default=None)
    ingest_run.add_argument("--max-jobs", type=int, default=None)
    ingest_show = ingest_subparsers.add_parser("show", help="Show an artifact record and manifest entry")
    ingest_show.add_argument("--vault", type=Path, default=vault_root())
    ingest_show.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_show.add_argument("artifact_id")
    ingest_audit = ingest_subparsers.add_parser("audit", help="Audit discovery, parsing, and extraction state")
    ingest_audit.add_argument("--vault", type=Path, default=vault_root())
    ingest_audit.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_batches = ingest_subparsers.add_parser("batches", help="List ingestion batches")
    ingest_batches.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_batches.add_argument("--limit", type=int, default=50)
    ingest_batches.add_argument("--status", default=None)
    ingest_batch = ingest_subparsers.add_parser("batch", help="Inspect or quarantine an ingestion batch")
    ingest_batch_subparsers = ingest_batch.add_subparsers(dest="ingest_batch_command", required=True)
    ingest_batch_show = ingest_batch_subparsers.add_parser("show", help="Show a batch summary")
    ingest_batch_show.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_batch_show.add_argument("batch_id")
    ingest_batch_audit = ingest_batch_subparsers.add_parser("audit", help="Audit a batch")
    ingest_batch_audit.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_batch_audit.add_argument("batch_id")
    ingest_batch_quarantine = ingest_batch_subparsers.add_parser("quarantine", help="Quarantine a batch")
    ingest_batch_quarantine.add_argument("--vault", type=Path, default=vault_root())
    ingest_batch_quarantine.add_argument("--db-path", type=Path, default=sqlite_path())
    ingest_batch_quarantine.add_argument("--reason", required=True)
    ingest_batch_quarantine.add_argument("batch_id")

    rebuild = subparsers.add_parser("rebuild-index", help="Rebuild SQLite and embeddings")
    rebuild.add_argument("--vault", type=Path, default=vault_root())

    health = subparsers.add_parser("health", help="Write a health report")
    health.add_argument("--vault", type=Path, default=vault_root())

    stale = subparsers.add_parser("stale", help="List stale state files")
    stale.add_argument("--vault", type=Path, default=vault_root())

    loops = subparsers.add_parser("loops", help="List active open loops")
    loops.add_argument("--vault", type=Path, default=vault_root())

    decay = subparsers.add_parser("decay", help="Surface confidence decay candidates (deterministic SQL)")
    decay.add_argument("--vault", type=Path, default=vault_root())

    review = subparsers.add_parser("review", help="Show queued draft items")
    review.add_argument("--vault", type=Path, default=vault_root())
    review_subparsers = review.add_subparsers(dest="review_command", required=False)
    review_batch = review_subparsers.add_parser("batch", help="Generate a batch review digest")
    review_batch.add_argument("--vault", type=Path, default=vault_root())
    review_batch.add_argument("--write", action="store_true")

    new = subparsers.add_parser("new", help="Create a new vault record")
    new_subparsers = new.add_subparsers(dest="new_command", required=True)

    new_entity_cmd = new_subparsers.add_parser("entity", help="Create a new entity record")
    new_entity_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_entity_cmd.add_argument("name")
    new_entity_cmd.add_argument("--subtype", default="person")
    new_entity_cmd.add_argument("--domain-primary", "--arena-primary", dest="domain_primary", default="cross_arena")
    new_entity_cmd.add_argument("--domain-secondary", "--arena-secondary", dest="domain_secondary", action="append", default=[])
    new_entity_cmd.add_argument("--privacy", default="personal")
    new_entity_cmd.add_argument("--significance", default="low")
    new_entity_cmd.add_argument("--summary", default=None)
    new_entity_cmd.add_argument("--canonical-name", default=None)
    new_entity_cmd.add_argument("--alias", action="append", default=[])
    new_entity_cmd.add_argument("--disambiguation", default=None)
    new_entity_cmd.add_argument("--compartment", action="append", default=[])
    new_entity_cmd.add_argument("--allowed-context", action="append", default=[])
    new_entity_cmd.add_argument("--blocked-context", action="append", default=[])
    new_entity_cmd.add_argument("--confidence", default="low")
    new_entity_cmd.add_argument("--confidence-basis", default="User-provided placeholder")
    new_entity_cmd.add_argument("--review-after", default=None)

    new_episode_cmd = new_subparsers.add_parser("episode", help="Create a new episode record")
    new_episode_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_episode_cmd.add_argument("title")
    new_episode_cmd.add_argument("--domain-primary", "--arena-primary", dest="domain_primary", default="cross_arena")
    new_episode_cmd.add_argument("--domain-secondary", "--arena-secondary", dest="domain_secondary", action="append", default=[])
    new_episode_cmd.add_argument("--privacy", default="personal")
    new_episode_cmd.add_argument("--significance", default="low")
    new_episode_cmd.add_argument("--source", default="manual")
    new_episode_cmd.add_argument("--summary", default=None)
    new_episode_cmd.add_argument("--entity", action="append", default=[])
    new_episode_cmd.add_argument("--evidence", action="append", default=[])
    new_episode_cmd.add_argument("--claim", action="append", default=[])
    new_episode_cmd.add_argument("--link", action="append", default=[])
    new_episode_cmd.add_argument("--confidence", default="low")
    new_episode_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_episode_cmd.add_argument("--review-after", default=None)
    new_episode_cmd.add_argument("--significance-rationale", default=None)

    new_decision_cmd = new_subparsers.add_parser("decision", help="Create a new decision record")
    new_decision_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_decision_cmd.add_argument("title")
    new_decision_cmd.add_argument("--domain-primary", "--arena-primary", dest="domain_primary", default="cross_arena")
    new_decision_cmd.add_argument("--domain-secondary", "--arena-secondary", dest="domain_secondary", action="append", default=[])
    new_decision_cmd.add_argument("--privacy", default="personal")
    new_decision_cmd.add_argument("--significance", default="low")
    new_decision_cmd.add_argument("--summary", default=None)
    new_decision_cmd.add_argument("--link", action="append", default=[])
    new_decision_cmd.add_argument("--confidence", default="low")
    new_decision_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_decision_cmd.add_argument("--review-after", default=None)
    new_decision_cmd.add_argument("--revisit-after", default=None)
    new_decision_cmd.add_argument("--revisit-condition", action="append", default=[])
    new_decision_cmd.add_argument("--alternative", action="append", default=[])

    new_loop_cmd = new_subparsers.add_parser("loop", help="Create a new open loop record")
    new_loop_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_loop_cmd.add_argument("title")
    new_loop_cmd.add_argument("--domain-primary", "--arena-primary", dest="domain_primary", default="cross_arena")
    new_loop_cmd.add_argument("--domain-secondary", "--arena-secondary", dest="domain_secondary", action="append", default=[])
    new_loop_cmd.add_argument("--privacy", default="personal")
    new_loop_cmd.add_argument("--significance", default="low")
    new_loop_cmd.add_argument("--summary", default=None)
    new_loop_cmd.add_argument("--link", action="append", default=[])
    new_loop_cmd.add_argument("--confidence", default="low")
    new_loop_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_loop_cmd.add_argument("--review-after", default=None)
    new_loop_cmd.add_argument("--priority", default="medium")
    new_loop_cmd.add_argument("--owner", default="user")
    new_loop_cmd.add_argument("--next-action", default="Describe the next action.")
    new_loop_cmd.add_argument("--blocked-by", default=None)

    new_knowledge_cmd = new_subparsers.add_parser("knowledge", help="Create a new knowledge record")
    new_knowledge_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_knowledge_cmd.add_argument("title")
    new_knowledge_cmd.add_argument("--category", default="frameworks")
    new_knowledge_cmd.add_argument("--domain-primary", "--arena-primary", dest="domain_primary", default="cross_arena")
    new_knowledge_cmd.add_argument("--domain-secondary", "--arena-secondary", dest="domain_secondary", action="append", default=[])
    new_knowledge_cmd.add_argument("--privacy", default="personal")
    new_knowledge_cmd.add_argument("--significance", default="low")
    new_knowledge_cmd.add_argument("--summary", default=None)
    new_knowledge_cmd.add_argument("--link", action="append", default=[])
    new_knowledge_cmd.add_argument("--confidence", default="low")
    new_knowledge_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_knowledge_cmd.add_argument("--review-after", default=None)

    new_evidence_cmd = new_subparsers.add_parser("evidence", help="Create a new evidence record")
    new_evidence_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_evidence_cmd.add_argument("title")
    new_evidence_cmd.add_argument("--source-type", default="manual_note")
    new_evidence_cmd.add_argument("--source-uri", default=None)
    new_evidence_cmd.add_argument("--artifact-ref", default=None)
    new_evidence_cmd.add_argument("--artifact-hash", default=None)
    new_evidence_cmd.add_argument("--artifact-timestamp", default=None)
    new_evidence_cmd.add_argument("--actor", action="append", default=[])
    new_evidence_cmd.add_argument("--arena", default="cross_arena")
    new_evidence_cmd.add_argument("--compartment", action="append", default=[])
    new_evidence_cmd.add_argument("--sensitivity", default="low")
    new_evidence_cmd.add_argument("--reliability", default="medium")
    new_evidence_cmd.add_argument("--privacy", default="personal")
    new_evidence_cmd.add_argument("--significance", default="low")
    new_evidence_cmd.add_argument("--summary", default=None)
    new_evidence_cmd.add_argument("--observed-fact", action="append", default=[])
    new_evidence_cmd.add_argument("--verbatim-excerpt", default=None)
    new_evidence_cmd.add_argument("--linked-claim", action="append", default=[])
    new_evidence_cmd.add_argument("--linked-episode", action="append", default=[])
    new_evidence_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_evidence_cmd.add_argument("--review-after", default=None)

    new_claim_cmd = new_subparsers.add_parser("claim", help="Create a new claim record")
    new_claim_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_claim_cmd.add_argument("claim_text")
    new_claim_cmd.add_argument("--claim-class", default="interpretation")
    new_claim_cmd.add_argument("--owner", default="user")
    new_claim_cmd.add_argument("--status", default="active")
    new_claim_cmd.add_argument("--confidence", type=float, default=0.5)
    new_claim_cmd.add_argument("--supporting-evidence", action="append", default=[])
    new_claim_cmd.add_argument("--contradicting-evidence", action="append", default=[])
    new_claim_cmd.add_argument("--linked-pattern", action="append", default=[])
    new_claim_cmd.add_argument("--first-seen", default=None)
    new_claim_cmd.add_argument("--last-reviewed", default=None)
    new_claim_cmd.add_argument("--review-notes", default="")
    new_claim_cmd.add_argument("--arena", default="cross_arena")
    new_claim_cmd.add_argument("--compartment", action="append", default=[])
    new_claim_cmd.add_argument("--privacy", default="personal")
    new_claim_cmd.add_argument("--significance", default="low")
    new_claim_cmd.add_argument("--summary", default=None)

    new_prediction_cmd = new_subparsers.add_parser("prediction", help="Record one prediction-ledger entry (WO-PSYCHE Ship 2)")
    new_prediction_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_prediction_cmd.add_argument("expectation")
    new_prediction_cmd.add_argument("--source", required=True, help="Id of the framework or pattern it derives from")
    new_prediction_cmd.add_argument("--review-after", required=True, help="YYYY-MM-DD or offset like '+14d'")
    new_prediction_cmd.add_argument("--trigger", default="", help="Condition under which it should be judged")
    new_prediction_cmd.add_argument("--subject", default=None)
    new_prediction_cmd.add_argument("--confidence", type=float, default=0.5)

    predictions_cmd = subparsers.add_parser("predictions", help="The prediction ledger: list entries, run the reconcile pass")
    predictions_subparsers = predictions_cmd.add_subparsers(dest="predictions_command", required=True)
    predictions_list = predictions_subparsers.add_parser("list", help="List ledger entries with verdicts")
    predictions_list.add_argument("--vault", type=Path, default=vault_root())
    predictions_list.add_argument("--pending", action="store_true", help="Only unscored entries")
    predictions_reconcile = predictions_subparsers.add_parser("reconcile", help="Score due predictions now")
    predictions_reconcile.add_argument("--vault", type=Path, default=vault_root())
    predictions_reconcile.add_argument("--db-path", type=Path, default=None)

    iip_cmd = subparsers.add_parser("iip", help="Interpersonal Interpretation Protocol instruments")
    iip_subparsers = iip_cmd.add_subparsers(dest="iip_command", required=True)
    iip_challenges = iip_subparsers.add_parser("challenges", help="Weekly counts from the IIP fire/challenge log")
    iip_challenges.add_argument("--vault", type=Path, default=vault_root())
    iip_challenges.add_argument("--weeks", type=int, default=4, help="How many weeks back to summarize")

    frameworks_cmd = subparsers.add_parser("frameworks", help="Owner-ratified interpretive frameworks (Tier R)")
    frameworks_subparsers = frameworks_cmd.add_subparsers(dest="frameworks_command", required=True)
    frameworks_list = frameworks_subparsers.add_parser("list", help="List ratified frameworks with predictive standing")
    frameworks_list.add_argument("--vault", type=Path, default=vault_root())
    frameworks_ratify = frameworks_subparsers.add_parser("ratify", help="Record a framework the owner has adopted")
    frameworks_ratify.add_argument("name")
    frameworks_ratify.add_argument("--summary", required=True, help="One paragraph: what the framework claims")
    frameworks_ratify.add_argument("--source", default=None, help="Where it comes from (book, document, conversation)")
    frameworks_ratify.add_argument("--vault", type=Path, default=vault_root())

    evidence = subparsers.add_parser("evidence", help="Evidence-specific operations")
    evidence_subparsers = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_correct = evidence_subparsers.add_parser("correct", help="Create an append-only evidence correction record")
    evidence_correct.add_argument("--vault", type=Path, default=vault_root())
    evidence_correct.add_argument("--path", type=Path, required=True)
    evidence_correct.add_argument("--field", required=True)
    evidence_correct.add_argument("--original", required=True)
    evidence_correct.add_argument("--corrected", required=True)
    evidence_correct.add_argument("--basis", required=True)
    evidence_correct.add_argument("--approved-by", default="user")

    new_state_cmd = new_subparsers.add_parser("state", help="Create a new state record")
    new_state_cmd.add_argument("--vault", type=Path, default=vault_root())
    new_state_cmd.add_argument("state_category", choices=["physical", "environmental", "financial", "relational", "work", "status", "appearance", "competence", "social_presence", "desirability"])
    new_state_cmd.add_argument("summary")
    new_state_cmd.add_argument("--state-secondary", "--arena-secondary", dest="state_secondary", action="append", default=[])
    new_state_cmd.add_argument("--privacy", default="personal")
    new_state_cmd.add_argument("--confidence", default="low")
    new_state_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_state_cmd.add_argument("--source", action="append", default=[])
    new_state_cmd.add_argument("--review-after", default=None)
    new_state_cmd.add_argument("--ttl-days", type=int, default=None)

    assemble = subparsers.add_parser("assemble", help="Assemble context for a query")
    assemble.add_argument("query", nargs="+")
    assemble.add_argument("--arena", "--domain", dest="domain", default=None)
    assemble.add_argument("--vault", type=Path, default=vault_root())

    heuristic = subparsers.add_parser("heuristic", help="Score text for memory processing")
    heuristic.add_argument("text", nargs="+")

    call = subparsers.add_parser("complete", help="Send a prompt through the provider router")
    call.add_argument("prompt", nargs="+")
    call.add_argument("--agent", default="writer")
    call.add_argument("--significance", default="medium")
    call.add_argument("--provider", default=None)
    call.add_argument("--model", default=None)

    sync = subparsers.add_parser("sync", help="Generate manifests, validate, rebuild index")
    sync.add_argument("--vault", type=Path, default=vault_root())

    agent = subparsers.add_parser("agent", help="Run a named agent against input text")
    agent.add_argument("name", choices=["advice", "analyst", "assembler", "dreamer", "elicitor", "interlocutor", "listener", "router", "skeptic", "writer"])
    agent.add_argument("input", nargs="+")
    agent.add_argument("--vault", type=Path, default=vault_root())
    agent.add_argument("--significance", default="medium")
    agent.add_argument("--task", default=None)
    agent.add_argument("--dry-run", action="store_true")
    agent.add_argument("--provider", default=None)
    agent.add_argument("--model", default=None)

    prompts = subparsers.add_parser("prompts", help="List available prompt templates")
    prompts.add_argument("--vault", type=Path, default=vault_root())

    prompt = subparsers.add_parser("prompt", help="Inspect a prompt template")
    prompt_subparsers = prompt.add_subparsers(dest="prompt_command", required=True)
    prompt_show = prompt_subparsers.add_parser("show", help="Show a prompt template")
    prompt_show.add_argument("name")

    transcript = subparsers.add_parser("transcript", help="Write an audit transcript entry")
    transcript_subparsers = transcript.add_subparsers(dest="transcript_command", required=True)
    transcript_append = transcript_subparsers.add_parser("append", help="Append a single transcript entry")
    transcript_append.add_argument("--vault", type=Path, default=vault_root())
    transcript_append.add_argument("--conversation-id", default=None)
    transcript_append.add_argument("--speaker", default="USER")
    transcript_append.add_argument("text", nargs="+")

    conversation = subparsers.add_parser("conversation", help="Inspect or manage Elicitor conversation state")
    conversation_subparsers = conversation.add_subparsers(dest="conversation_command", required=True)
    conversation_show = conversation_subparsers.add_parser("show", help="Show the current narrative state")
    conversation_show.add_argument("--vault", type=Path, default=vault_root())
    conversation_show.add_argument("--conversation-id", default=None)
    conversation_history_cmd = conversation_subparsers.add_parser("history", help="Show recent transcript turns")
    conversation_history_cmd.add_argument("--vault", type=Path, default=vault_root())
    conversation_history_cmd.add_argument("--conversation-id", default=None)
    conversation_reset = conversation_subparsers.add_parser("reset", help="Clear persisted narrative state")
    conversation_reset.add_argument("--vault", type=Path, default=vault_root())
    conversation_reset.add_argument("--conversation-id", default=None)
    conversation_digest = conversation_subparsers.add_parser("digest", help="Write a conversation digest report")
    conversation_digest.add_argument("--vault", type=Path, default=vault_root())
    conversation_digest.add_argument("--conversation-id", default=None)

    capture = subparsers.add_parser("capture", help="Capture input and create a review draft when warranted")
    capture.add_argument("--vault", type=Path, default=vault_root())
    capture.add_argument("--conversation-id", default=None)
    capture.add_argument("--speaker", default="USER")
    capture.add_argument("--provider", default=None)
    capture.add_argument("--model", default=None)
    capture_output = capture.add_mutually_exclusive_group()
    capture_output.add_argument(
        "--quiet",
        action="store_true",
        help="Print only Lisan's spoken response to stdout (default for interactive use).",
    )
    capture_output.add_argument(
        "--verbose",
        action="store_true",
        help="Print the full pipeline JSON bundle. Useful for debugging.",
    )
    capture.add_argument("text", nargs="+")

    primer_audit = subparsers.add_parser("primer-audit", help="Run the yearly primer audit scaffold")
    primer_audit.add_argument("--vault", type=Path, default=vault_root())
    primer_audit.add_argument("--dry-run", action="store_true")
    primer_audit.add_argument("--provider", default="anthropic")
    primer_audit.add_argument("--model", default=None)

    migrate = subparsers.add_parser("migrate", help="Inspect or run vault structure migrations")
    migrate.add_argument("--vault", type=Path, default=vault_root())
    migrate.add_argument("--apply", action="store_true")

    edit = subparsers.add_parser("edit", help="Edit an existing record")
    edit.add_argument("--path", type=Path, required=True)
    edit.add_argument("--set", action="append", default=[])
    edit.add_argument("--add", action="append", default=[])
    edit.add_argument("--append-body", default=None)

    archive = subparsers.add_parser("archive", help="Archive completed records")
    archive_subparsers = archive.add_subparsers(dest="archive_command", required=True)
    archive_loop = archive_subparsers.add_parser("loop", help="Archive a resolved open loop")
    archive_loop.add_argument("--path", type=Path, required=True)
    archive_loop.add_argument("--force", action="store_true")

    backup = subparsers.add_parser("backup", help="Create or test local backups")
    backup_subparsers = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_subparsers.add_parser("create", help="Create a backup archive")
    backup_create.add_argument("--vault", type=Path, default=vault_root())
    backup_create.add_argument("--destination", type=Path, default=None)
    backup_create.add_argument("--recipient", default=None)
    backup_create.add_argument("--identity", default=None)
    backup_create.add_argument("--encrypt", action="store_true")
    backup_create.add_argument("--test-restore", action="store_true")
    backup_test = backup_subparsers.add_parser("test", help="Test restoring a backup archive")
    backup_test.add_argument("--vault", type=Path, default=vault_root())
    backup_test.add_argument("--destination", type=Path, default=None)
    backup_test.add_argument("--archive", type=Path, default=None)
    backup_test.add_argument("--identity", default=None)
    backup_status_cmd = backup_subparsers.add_parser("status", help="Show backup status")
    backup_status_cmd.add_argument("--vault", type=Path, default=vault_root())
    backup_status_cmd.add_argument("--destination", type=Path, default=None)

    entity = subparsers.add_parser("entity", help="Entity-specific operations")
    entity_subparsers = entity.add_subparsers(dest="entity_command", required=True)
    entity_epoch = entity_subparsers.add_parser("epoch", help="Archive the current entity epoch and start a new one")
    entity_epoch.add_argument("--path", type=Path, required=True)
    entity_epoch.add_argument("--summary", required=True)
    entity_epoch.add_argument("--disambiguation", default=None)

    draft = subparsers.add_parser("draft", help="Manage queued drafts")
    draft_subparsers = draft.add_subparsers(dest="draft_command", required=True)
    draft_review = draft_subparsers.add_parser("review", help="Review a draft with Skeptic and Interlocutor")
    draft_review.add_argument("--vault", type=Path, default=vault_root())
    draft_review.add_argument("--path", type=Path, required=True)
    draft_review.add_argument("--provider", default=None)
    draft_review.add_argument("--model", default=None)
    draft_review.add_argument("--apply", action="store_true")
    draft_promote = draft_subparsers.add_parser("promote", help="Promote a draft file into an episode")
    draft_promote.add_argument("--vault", type=Path, default=vault_root())
    draft_promote.add_argument("--path", type=Path, required=True)
    draft_backlog = draft_subparsers.add_parser(
        "promote-backlog", help="Promote all skeptic-approved episode drafts left behind before auto-promotion"
    )
    draft_backlog.add_argument("--vault", type=Path, default=vault_root())
    draft_backlog.add_argument("--db-path", type=Path, default=None)

    checkin_cmd = subparsers.add_parser("checkin", help="Record a neutral observational check-in about a person")
    checkin_cmd.add_argument("person", help="Who the observation is about")
    checkin_cmd.add_argument("--note", required=True, help="What was observed (never interpretation)")
    checkin_cmd.add_argument("--tag", action="append", default=[], help="Context tag (repeatable)")
    checkin_cmd.add_argument("--quote", default=None, help="Optional verbatim quote")
    checkin_cmd.add_argument("--vault", type=Path, default=vault_root())
    checkin_cmd.add_argument("--db-path", type=Path, default=None)

    support_cmd = subparsers.add_parser("support", help="Log a support-strategy outcome, or list what helps")
    support_cmd.add_argument("person", help="Who the strategy is for")
    support_cmd.add_argument("--strategy", default=None, help="The strategy tried (omit to list what helps)")
    support_cmd.add_argument("--outcome", default=None, choices=["worked", "didnt_work", "mixed"])
    support_cmd.add_argument("--note", default=None, help="Optional context for this outcome")
    support_cmd.add_argument("--vault", type=Path, default=vault_root())
    support_cmd.add_argument("--db-path", type=Path, default=None)

    restart = subparsers.add_parser("restart", help="Restart the resident service (refuses over in-flight jobs)")
    restart.add_argument("--db-path", type=Path, default=None)
    restart.add_argument("--force", action="store_true", help="Restart even while jobs are mid-run")

    logs = subparsers.add_parser("logs", help="Show recent log entries")
    logs.add_argument("--vault", type=Path, default=vault_root())
    logs.add_argument("--tail", type=int, default=50, metavar="N")
    logs.add_argument("--errors", action="store_true", help="Only warnings and errors (logs/errors.log)")

    traces = subparsers.add_parser("traces", help="Inspect recent chat turn traces")
    traces_subparsers = traces.add_subparsers(dest="traces_command", required=True)
    traces_recent = traces_subparsers.add_parser("recent", help="Show recent turn traces")
    traces_recent.add_argument("--db-path", type=Path, default=None)
    traces_recent.add_argument("--limit", type=int, default=20)
    traces_show = traces_subparsers.add_parser("show", help="Show one turn trace")
    traces_show.add_argument("--db-path", type=Path, default=None)
    traces_show.add_argument("turn_id")

    dreamer = subparsers.add_parser("dreamer", help="Run Dreamer maintenance analyses")
    dreamer_subparsers = dreamer.add_subparsers(dest="dreamer_command", required=True)
    for task_name in ["compress", "primer", "contradict", "confidence", "epoch", "overfitting", "identity-anchor", "reconcile", "hindsight"]:
        dreamer_task = dreamer_subparsers.add_parser(task_name, help=f"Run the Dreamer {task_name} task")
        dreamer_task.add_argument("--vault", type=Path, default=vault_root())
        dreamer_task.add_argument("--provider", default=None)
        dreamer_task.add_argument("--model", default=None)

    analyst = subparsers.add_parser("analyst", help="Run longitudinal pattern analysis")
    analyst_subparsers = analyst.add_subparsers(dest="analyst_command", required=True)
    analyst_scan = analyst_subparsers.add_parser("scan", help="Scan for recurring pattern hypotheses")
    analyst_scan.add_argument("--vault", type=Path, default=vault_root())
    analyst_scan.add_argument("--provider", default=None)
    analyst_scan.add_argument("--model", default=None)

    patterns = subparsers.add_parser("patterns", help="Inspect pattern governance")
    patterns_subparsers = patterns.add_subparsers(dest="patterns_command", required=True)
    patterns_audit = patterns_subparsers.add_parser("audit", help="Audit pattern lifecycle and Dreamer eligibility")
    patterns_audit.add_argument("--vault", type=Path, default=vault_root())

    jobs = subparsers.add_parser("jobs", help="Inspect and run the durable background job queue")
    jobs_subparsers = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_run = jobs_subparsers.add_parser("run", help="Run queued jobs until the queue is empty")
    jobs_run.add_argument("--vault", type=Path, default=vault_root())
    jobs_run.add_argument("--db-path", type=Path, default=None)
    jobs_run.add_argument("--provider", default=None)
    jobs_run.add_argument("--model", default=None)
    jobs_run.add_argument("--worker-id", default=None)
    jobs_run.add_argument("--max-jobs", type=int, default=None)
    jobs_list = jobs_subparsers.add_parser("list", help="List jobs")
    jobs_list.add_argument("--db-path", type=Path, default=None)
    jobs_list.add_argument("--status", default=None)
    jobs_list.add_argument("--limit", type=int, default=50)
    jobs_show = jobs_subparsers.add_parser("show", help="Show a job record")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--db-path", type=Path, default=None)
    jobs_retry = jobs_subparsers.add_parser("retry", help="Move a failed/retry_wait job back to queued")
    jobs_retry.add_argument("job_id")
    jobs_retry.add_argument("--db-path", type=Path, default=None)
    jobs_cancel = jobs_subparsers.add_parser("cancel", help="Cancel a job")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.add_argument("--db-path", type=Path, default=None)
    jobs_audit = jobs_subparsers.add_parser("audit", help="Audit queue health and failure state")
    jobs_audit.add_argument("--db-path", type=Path, default=None)
    jobs_audit.add_argument("--vault", type=Path, default=vault_root())
    jobs_reap = jobs_subparsers.add_parser("reap-stuck", help="Move stale running jobs back to retry_wait or failed")
    jobs_reap.add_argument("--db-path", type=Path, default=None)
    jobs_reap.add_argument("--timeout-minutes", type=int, default=15)
    jobs_reap.add_argument("--fail", action="store_true")

    provider_cmd = subparsers.add_parser("provider", help="Inspect provider readiness and diagnostics")
    provider_subparsers = provider_cmd.add_subparsers(dest="provider_command", required=True)
    provider_check = provider_subparsers.add_parser("check", help="Run provider preflight diagnostics")
    provider_check.add_argument("--provider", default=None)
    provider_check.add_argument("--model", default=None)

    skills_cmd = subparsers.add_parser("skills", help="List, install, or set up conversation skills (tools)")
    skills_subparsers = skills_cmd.add_subparsers(dest="skills_command", required=True)
    skills_subparsers.add_parser("list", help="Show bundled and installed skills")
    skills_install = skills_subparsers.add_parser("install", help="Install a bundled skill into the active skills directory")
    skills_install.add_argument("name", nargs="?", default=None, help="Skill name (omit with --all)")
    skills_install.add_argument("--all", action="store_true", help="Install every bundled skill")
    skills_install.add_argument("--force", action="store_true", help="Overwrite an existing installed copy")
    skills_uninstall = skills_subparsers.add_parser("uninstall", help="Remove an installed skill")
    skills_uninstall.add_argument("name")
    skills_auth = skills_subparsers.add_parser(
        "auth",
        help="Authorize a skill's account access (google/gmail/calendar/drive): prints the consent URL, then exchange the pasted redirect URL",
    )
    skills_auth.add_argument("name", help="Skill or provider: gmail, google, calendar, drive")
    skills_auth.add_argument("pasted_url", nargs="?", default=None,
                             help="The full URL your browser landed on after consent (second step)")

    skills_setup = skills_subparsers.add_parser(
        "setup",
        help="Run a skill's credential/setup script (e.g. lisan skills setup gmail_search -- --check)",
    )
    skills_setup.add_argument("name")
    skills_setup.add_argument("setup_args", nargs=argparse.REMAINDER, help="Arguments forwarded to the setup script")

    telegram_cmd = subparsers.add_parser("telegram", help="Talk to Lisan over a Telegram bot")
    telegram_subparsers = telegram_cmd.add_subparsers(dest="telegram_command", required=True)
    telegram_setup = telegram_subparsers.add_parser("setup", help="Interactive wizard: token + allowlist")
    telegram_run = telegram_subparsers.add_parser("run", help="Start the Telegram long-poll bot (Ctrl-C to stop)")
    telegram_run.add_argument("--vault", type=Path, default=vault_root())
    telegram_run.add_argument("--provider", default=None)
    telegram_run.add_argument("--model", default=None)
    telegram_install = telegram_subparsers.add_parser("install-service", help="Install + start an always-on service (launchd/systemd)")
    telegram_install.add_argument("--vault", type=Path, default=vault_root())
    telegram_subparsers.add_parser("uninstall-service", help="Stop + remove the always-on service")

    scheduler_cmd = subparsers.add_parser("scheduler", help="Run tasks at their scheduled times")
    scheduler_subparsers = scheduler_cmd.add_subparsers(dest="scheduler_command", required=True)
    scheduler_run = scheduler_subparsers.add_parser("run", help="Run the scheduler loop in the foreground (Ctrl-C to stop)")
    scheduler_run.add_argument("--vault", type=Path, default=vault_root())
    scheduler_run.add_argument("--db-path", type=Path, default=None)
    scheduler_run.add_argument("--provider", default=None)
    scheduler_run.add_argument("--model", default=None)
    scheduler_run.add_argument("--poll", type=float, default=30.0, help="Max seconds between due-work checks")
    scheduler_install = scheduler_subparsers.add_parser("install-service", help="Install + start the always-on scheduler (launchd/systemd)")
    scheduler_install.add_argument("--vault", type=Path, default=vault_root())
    scheduler_subparsers.add_parser("uninstall-service", help="Stop + remove the always-on scheduler")

    task_cmd = subparsers.add_parser("task", help="Schedule tasks to run at a future time")
    task_subparsers = task_cmd.add_subparsers(dest="task_command", required=True)
    task_add = task_subparsers.add_parser("add", help="Schedule a task")
    task_add.add_argument("text", help="What to do: the reminder message, prompt, or codex task")
    task_add.add_argument("--kind", choices=["reminder", "prompt", "codex"], default="reminder")
    task_add.add_argument("--at", default=None, help="When to fire: 'YYYY-MM-DD HH:MM' (local), ISO 8601, or 'HH:MM'")
    task_add.add_argument("--every", default=None, help="Recur every interval, e.g. 30m, 2h, 1d, 1w")
    task_add.add_argument("--daily", default=None, help="Recur daily at HH:MM local time")
    task_add.add_argument("--dir", dest="working_directory", default=None, help="Working directory (codex tasks)")
    task_add.add_argument("--db-path", type=Path, default=None)
    task_list = task_subparsers.add_parser("list", help="List scheduled tasks")
    task_list.add_argument("--db-path", type=Path, default=None)
    task_cancel = task_subparsers.add_parser("cancel", help="Cancel a scheduled task")
    task_cancel.add_argument("job_id")
    task_cancel.add_argument("--db-path", type=Path, default=None)

    plan_cmd = subparsers.add_parser("plan", help="Durable multi-step background plans")
    plan_subparsers = plan_cmd.add_subparsers(dest="plan_command", required=True)
    plan_add = plan_subparsers.add_parser("add", help="Create a plan")
    plan_add.add_argument("goal", help="What the plan achieves")
    plan_add.add_argument("--step", action="append", required=True, dest="steps",
                          help="A step as 'kind: description' (kind: codex|prompt|note); repeatable, runs in order")
    plan_add.add_argument("--dir", dest="working_directory", default=None)
    plan_add.add_argument("--db-path", type=Path, default=None)
    plan_list = plan_subparsers.add_parser("list", help="List plans")
    plan_list.add_argument("--db-path", type=Path, default=None)
    plan_cancel = plan_subparsers.add_parser("cancel", help="Cancel an active plan")
    plan_cancel.add_argument("plan_id")
    plan_cancel.add_argument("--db-path", type=Path, default=None)
    plan_ingest = plan_subparsers.add_parser("ingest-folder", help="Autonomously ingest a folder of notes, in batches, surfacing questions")
    plan_ingest.add_argument("path", type=Path)
    plan_ingest.add_argument("--batch", type=int, default=6, help="Files per codex step")
    plan_ingest.add_argument("--limit", type=int, default=None, help="Only the first N files (for a trial run)")
    plan_ingest.add_argument("--db-path", type=Path, default=None)

    browser_cmd = subparsers.add_parser("browser", help="The agent's own persistent, shared Chrome session")
    browser_sub = browser_cmd.add_subparsers(dest="browser_command", required=True)
    browser_sub.add_parser("open", help="Launch (or focus) the agent's browser")
    browser_sub.add_parser("status", help="Is the agent's browser running?")
    browser_goto = browser_sub.add_parser("goto", help="Navigate the agent's browser")
    browser_goto.add_argument("url")
    browser_sub.add_parser("read", help="Print the current page's text")

    entities_cmd = subparsers.add_parser("entities", help="Entity maintenance: find and merge duplicates")
    entities_sub = entities_cmd.add_subparsers(dest="entities_command", required=True)
    entities_dedup = entities_sub.add_parser("dedup", help="List same-kind near-duplicate entities worth merging")
    entities_merge = entities_sub.add_parser("merge", help="Merge one entity into another (content absorbed, fragment archived, names become aliases)")
    entities_merge.add_argument("source", help="Entity to absorb (name, id, or file stem)")
    entities_merge.add_argument("target", help="Entity that survives")

    deviations_cmd = subparsers.add_parser("deviations", help="The agent's own aches: deviations detected in its model of the world and itself")
    deviations_sub = deviations_cmd.add_subparsers(dest="deviations_command", required=True)
    deviations_sub.add_parser("scan", help="Detect deviations, satiate healed ones, emit new self-loops (capped)")
    deviations_sub.add_parser("list", help="Show active self-originated loops")
    self_eval_note = "Review my own recent real conversations and machinery; write a private report; suggest improvements"

    self_cmd = subparsers.add_parser("self", help="The agent's generated self-model and live state")
    self_subparsers = self_cmd.add_subparsers(dest="self_command", required=True)
    self_subparsers.add_parser("evaluate", help=self_eval_note)
    self_manifest = self_subparsers.add_parser("manifest", help="Show the generated capability manifest")
    self_manifest.add_argument("--json", action="store_true", dest="as_json")
    self_state_cmd = self_subparsers.add_parser("state", help="Show live operational state")
    self_state_cmd.add_argument("--vault", type=Path, default=vault_root())
    self_state_cmd.add_argument("--db-path", type=Path, default=None)
    self_state_cmd.add_argument("--json", action="store_true", dest="as_json")
    self_primer = self_subparsers.add_parser("primer", help="Regenerate primer/capabilities.md")
    self_primer.add_argument("--vault", type=Path, default=vault_root())
    self_primer.add_argument("--force", action="store_true")
    self_extract = self_subparsers.add_parser(
        "extract-voice", help="Distill candidate voice invariants from transcript history"
    )
    self_extract.add_argument("--vault", type=Path, default=vault_root())
    self_extract.add_argument("--provider", default=None)
    self_extract.add_argument("--model", default=None)
    self_extract.add_argument("--out", type=Path, default=None)
    self_extract.add_argument("--min-invariants", type=int, default=None)
    self_extract.add_argument("--min-conversations", type=int, default=None)
    self_extract.add_argument("--max-turns", type=int, default=150)
    self_ratify = self_subparsers.add_parser(
        "ratify", help="Ratify an extraction artifact into the kernel voice (ceremony)"
    )
    self_ratify.add_argument("--vault", type=Path, default=vault_root())
    self_ratify.add_argument("--from", dest="artifact", type=Path, required=True)
    self_ratify.add_argument("--provisional", action="store_true",
                             help="Agent-ratified pending owner review (provenance-marked)")
    self_backfill = self_subparsers.add_parser(
        "backfill-episodes", help="Assemble first-person episodes from existing system records"
    )
    self_backfill.add_argument("--vault", type=Path, default=vault_root())
    self_backfill.add_argument("--db-path", type=Path, default=None)
    self_extract_beliefs = self_subparsers.add_parser(
        "extract-beliefs", help="Deterministic belief candidates from first-person episodes (WO-10)"
    )
    self_extract_beliefs.add_argument("--vault", type=Path, default=vault_root())
    self_extract_beliefs.add_argument("--out", type=Path, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "checkin":
        from .tools.checkin import record_checkin
        out = record_checkin(args.vault, args.person, args.note, tags=args.tag, quote=args.quote, db_path=args.db_path)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out.get("ok") else 1

    if args.command == "support":
        from .tools.checkin import support_note, support_summary
        if args.strategy and args.outcome:
            out = support_note(args.vault, args.person, args.strategy, args.outcome, note=args.note, db_path=args.db_path)
        elif not args.strategy and not args.outcome:
            out = support_summary(args.vault, args.person)
        else:
            out = {"ok": False, "error": "give both --strategy and --outcome to log, or neither to list"}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out.get("ok") else 1

    if args.command == "restart":
        from .tools.restart import render_restart_report, restart_service
        report = restart_service(db_path=args.db_path, force=args.force)
        print(render_restart_report(report))
        return 0 if report.get("restarted") or report.get("reason") == "jobs_in_flight" else 1

    if args.command == "logs":
        from .tools.log import tail_log
        print(tail_log(args.vault, lines=args.tail, errors_only=args.errors))
        return 0

    if args.command == "traces":
        if args.traces_command == "recent":
            traces = list_recent_turn_traces(limit=args.limit, db_path=args.db_path)
            print(format_recent_turn_traces(traces))
            return 0
        if args.traces_command == "show":
            trace = load_turn_trace(args.turn_id, db_path=args.db_path)
            if trace is None:
                print(f"Missing trace: {args.turn_id}", file=sys.stderr)
                return 1
            print(format_turn_trace(trace))
            return 0

    if args.command == "chat":
        return run_chat(
            vault=args.vault,
            conversation_id=args.conversation_id,
            provider=args.provider,
            model=args.model,
            trace=args.trace,
        )

    if args.command == "init":
        vault = vault_root()
        ensure_repo_layout()
        seeded = write_seed_files(vault)
        print(f"Lisan workspace initialized at {vault}.")
        if seeded:
            print("\nSeed files created (fill these in before first use):")
            for f in seeded:
                print(f"  {f}")
        from .tools.onboarding import run_onboarding
        run_onboarding(vault)
        if not os.environ.get("LISAN_VAULT"):
            print(
                "\nWARNING: LISAN_VAULT is not set.\n"
                "Personal memories will be written to the repo-local vault and may be\n"
                "accidentally committed to git. Set LISAN_VAULT to an external path\n"
                "before capturing real data:\n\n"
                '  export LISAN_VAULT="$HOME/.local/share/Lisan/vault"\n'
            )
        return 0

    if args.command == "purge":
        resolved = _resolve_purge_options(args)
        if resolved is None:
            print("Purge cancelled.")
            return 1
        preserve_config, backup_before = resolved
        result = purge_installation(
            preserve_config=preserve_config,
            preserve_kernel=bool(getattr(args, "preserve_kernel", False)),
            backup_before=backup_before,
            backup_destination=args.backup_destination,
        )
        print(f"Purged vault: {result.vault}")
        if getattr(result, "kernel_preserved", False):
            print("Identity kernel preserved (primer/identity-core.md).")
        if result.backup_created and result.backup_archive_path:
            print(f"Backup created: {result.backup_archive_path}")
        if result.removed_paths:
            print("Removed:")
            for path in result.removed_paths:
                print(f"  {path}")
        if result.seeded_files:
            print("Recreated seed files:")
            for path in result.seeded_files:
                print(f"  {path}")
        if preserve_config:
            print("Config preserved.")
        else:
            print("Config reset to defaults.")
        return 0

    if args.command == "uninstall":
        keep_vault = not args.purge_vault
        if args.yes:
            confirmed = True
        else:
            confirmed = _confirm_uninstall(keep_vault=keep_vault, install_root=args.home or default_install_root(), bin_dir=args.bin_dir or default_bin_dir())
        if not confirmed:
            print("Uninstall cancelled.")
            return 1
        result = uninstall_installation(
            install_root=args.home,
            bin_dir=args.bin_dir,
            keep_vault=keep_vault,
        )
        print(f"Removed launcher and app files from: {result.install_root}")
        if result.removed_paths:
            print("Removed:")
            for path in result.removed_paths:
                print(f"  {path}")
        if result.removed_path_entries:
            print("Removed shell PATH entry from:")
            for path in result.removed_path_entries:
                print(f"  {path}")
        if result.kept_vault:
            print(f"Kept vault: {result.vault}")
            print("If you want a full reset later, run: lisan purge")
        else:
            print("Vault removed.")
        return 0

    if args.command == "validate":
        report = validate_vault(args.vault)
        print(format_report(report))
        return 0 if report.ok else 1

    if args.command == "adjutant":
        from .tools.adjutant_runner import (
            adjutant_status,
            format_cycle_result,
            format_log,
            format_status,
            run_cycle,
            tail_log,
        )

        if args.adjutant_command == "run":
            result = run_cycle(args.vault, args.db_path)
            print(format_cycle_result(result))
            return 1 if result["halted"] else 0
        if args.adjutant_command == "status":
            status = adjutant_status(args.vault, args.db_path)
            print(format_status(status))
            return 0 if status["intent_valid"] and not status["halted"] else 1
        if args.adjutant_command == "log":
            print(format_log(tail_log(args.db_path, args.limit)))
            return 0

    if args.command == "confirm":
        from .tools.adjutant_confirmations import (
            approve_confirmation,
            deny_confirmation,
            format_pending,
            list_pending,
        )

        if args.confirm_command == "list":
            print(format_pending(args.vault, list_pending(args.db_path)))
            return 0
        try:
            if args.confirm_command == "approve":
                outcome = approve_confirmation(args.vault, args.id, db_path=args.db_path)
                print(f"Approved {outcome['id']} (task {outcome['task_id']}). It executes on the next cycle.")
                return 0
            if args.confirm_command == "deny":
                outcome = deny_confirmation(args.vault, args.id, db_path=args.db_path)
                print(f"Denied {outcome['id']} (task {outcome['task_id']}).")
                return 0
        except (KeyError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.command == "intent":
        from .tools.intent import (
            CAPABILITIES,
            IntentError,
            edit_intent,
            format_intent,
            format_intent_history,
            init_intent,
            load_intent,
            resolve_delegation,
        )

        if args.intent_command == "show":
            print(format_intent(args.vault))
            return 0
        if args.intent_command == "init":
            try:
                path = init_intent(args.vault, force=args.force)
            except IntentError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Created {path}. Edit it: lisan intent edit")
            return 0
        if args.intent_command == "edit":
            try:
                result = edit_intent(args.vault)
            except IntentError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if not result["changed"]:
                print(f"No change. Version stays at {result['version']}.")
                return 0
            print(f"Snapshot: {result['snapshot']}")
            print(f"Version bumped to {result['version']}.")
            if result["issues"]:
                print(f"WARNING: intent.md is now invalid ({len(result['issues'])} issue(s)); "
                      "the Adjutant will refuse to start until fixed:", file=sys.stderr)
                for issue in result["issues"]:
                    print(f"  - {issue}", file=sys.stderr)
                return 1
            return 0
        if args.intent_command == "history":
            print(format_intent_history(args.vault))
            return 0
        if args.intent_command == "check":
            if args.capability not in CAPABILITIES:
                print(f"Unknown capability {args.capability!r}. Known: {', '.join(sorted(CAPABILITIES))}", file=sys.stderr)
                return 1
            try:
                intent = load_intent(args.vault)
            except IntentError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            verdict = resolve_delegation(intent.delegations, args.arena, args.capability)
            print(f"{verdict.decision.upper()}  (rule: {verdict.rule}, intent version {intent.version})")
            for reason in verdict.reasons:
                print(f"  - {reason}")
            return 0

    if args.command == "manifest":
        manifests = generate_manifests(args.vault, write=not args.no_write)
        print("\n".join(sorted(manifests)))
        return 0

    if args.command == "ingest":
        if args.ingest_command is None and args.reference is not None:
            try:
                result = ingest_reference_sources(
                    list(args.reference),
                    vault=args.vault,
                    db_path=args.db_path,
                    replace=args.replace,
                    on_exists=args.on_exists,
                    link_entities=args.link_entity,
                    plan_only=args.plan,
                )
            except (FileExistsError, RuntimeError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if args.plan:
                if args.json:
                    print(json.dumps(result, indent=2, default=str))
                else:
                    print(format_reference_ingest_plan(result))
            else:
                print(json.dumps(result, indent=2, default=str))
            return 0
        if args.ingest_command == "scan":
            include_ext = _split_csv_values(args.include_ext)
            exclude_ext = _split_csv_values(args.exclude_ext)
            if args.allow_sealed:
                print("WARNING: --allow-sealed is dangerous and should only be used for explicit, intentional imports.", file=sys.stderr)
            if args.review:
                preview = plan_scan_path(
                    args.path,
                    vault=args.vault,
                    db_path=args.db_path,
                    max_file_size_bytes=int(args.max_size_mb * 1024 * 1024) if args.max_size_mb else None,
                    include_ext=include_ext,
                    exclude_ext=exclude_ext,
                    include_hidden=args.include_hidden,
                    allow_restricted=args.allow_restricted,
                    allow_high=args.allow_high,
                    allow_sealed=args.allow_sealed,
                )
                if args.json:
                    print(json.dumps(preview, indent=2, default=str))
                else:
                    print(format_ingest_plan(preview))
                prompt = (
                    "Proceed with the actual ingest writes? "
                    f"(files={preview['summary']['total_files_seen']}, "
                    f"would_ingest={preview['summary']['would_ingest']}, "
                    f"would_skip={preview['summary']['would_skip']}) [y/N]: "
                )
                answer = input(prompt).strip().lower()
                if answer not in {"y", "yes"}:
                    print("Ingest review declined. No writes performed.")
                    return 0
                result = scan_path(
                    args.path,
                    vault=args.vault,
                    db_path=args.db_path,
                    queue_jobs=True,
                    max_file_size_bytes=int(args.max_size_mb * 1024 * 1024) if args.max_size_mb else None,
                    dry_run=False,
                    include_ext=include_ext,
                    exclude_ext=exclude_ext,
                    include_hidden=args.include_hidden,
                    allow_restricted=args.allow_restricted,
                    allow_high=args.allow_high,
                    allow_sealed=args.allow_sealed,
                    batch_mode="review",
                )
                print(json.dumps(result, indent=2, default=str))
                return 0
            if args.dry_run:
                result = scan_path(
                    args.path,
                    vault=args.vault,
                    db_path=args.db_path,
                    queue_jobs=False,
                    max_file_size_bytes=int(args.max_size_mb * 1024 * 1024) if args.max_size_mb else None,
                    dry_run=True,
                    include_ext=include_ext,
                    exclude_ext=exclude_ext,
                    include_hidden=args.include_hidden,
                    allow_restricted=args.allow_restricted,
                    allow_high=args.allow_high,
                    allow_sealed=args.allow_sealed,
                )
                if args.json:
                    print(json.dumps(result, indent=2, default=str))
                else:
                    print(format_ingest_plan(result))
                return 0
            result = scan_path(
                args.path,
                vault=args.vault,
                db_path=args.db_path,
                queue_jobs=True,
                max_file_size_bytes=int(args.max_size_mb * 1024 * 1024) if args.max_size_mb else None,
                include_ext=include_ext,
                exclude_ext=exclude_ext,
                include_hidden=args.include_hidden,
                allow_restricted=args.allow_restricted,
                allow_high=args.allow_high,
                allow_sealed=args.allow_sealed,
                batch_mode="scan",
            )
            print(json.dumps(result, indent=2, default=str))
            return 0
        if args.ingest_command == "status":
            report = audit_ingestion(vault=args.vault, db_path=args.db_path)
            print(format_ingest_status(report))
            return 0
        if args.ingest_command == "run":
            result = run_jobs_worker(
                vault=args.vault,
                db_path=args.db_path,
                provider=args.provider,
                model=args.model,
                worker_id=args.worker_id,
                max_jobs=args.max_jobs,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0
        if args.ingest_command == "show":
            result = show_artifact(args.artifact_id, vault=args.vault, db_path=args.db_path)
            if result is None:
                print(f"Missing artifact: {args.artifact_id}", file=sys.stderr)
                return 1
            print(json.dumps(result, indent=2, default=str))
            return 0
        if args.ingest_command == "audit":
            report = audit_ingestion(vault=args.vault, db_path=args.db_path)
            print(format_ingest_audit(report))
            return 0
        if args.ingest_command == "batches":
            batches = list_batches(limit=args.limit, status=args.status, db_path=args.db_path)
            print(format_ingest_batches(batches))
            return 0
        if args.ingest_command == "batch":
            if args.ingest_batch_command == "show":
                report = summarize_batch(args.batch_id, vault=args.vault, db_path=args.db_path)
                print(format_ingest_batch_summary(report))
                return 0
            if args.ingest_batch_command == "audit":
                report = summarize_batch(args.batch_id, vault=args.vault, db_path=args.db_path)
                print(format_ingest_batch_audit(report))
                return 0
            if args.ingest_batch_command == "quarantine":
                report = quarantine_batch(args.batch_id, args.reason, vault=args.vault, db_path=args.db_path)
                if report is None:
                    print(f"Missing batch: {args.batch_id}", file=sys.stderr)
                    return 1
                print(format_ingest_batch_summary(report))
                return 0
        if args.ingest_command is None:
            print("ingest requires a subcommand or --reference", file=sys.stderr)
            return 1

    if args.command == "rebuild-index":
        counts = rebuild_index(args.vault)
        print(json.dumps(counts, indent=2))
        return 0

    if args.command == "health":
        _bootstrap_runtime(args.vault, ensure_schema=True)
        report = generate_health_report(args.vault)
        out = args.vault / "reports" / "health-latest.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(out)
        return 0

    if args.command == "stale":
        from datetime import date

        from .frontmatter import load_markdown

        for path in sorted((args.vault / "state").glob("*.md")):
            try:
                doc = load_markdown(path)
            except Exception as exc:
                log_error(args.vault, f"stale state load failed for {path}", exc)
                continue
            ttl = int(doc.frontmatter.get("ttl_days", 0) or 0)
            updated = doc.frontmatter.get("updated")
            if not ttl or not updated:
                continue
            try:
                age = (date.today() - date.fromisoformat(str(updated))).days
            except ValueError:
                continue
            if age > ttl:
                print(f"{path.name}: age={age} ttl={ttl}")
        for path in sorted((args.vault / "open_loops").glob("*.md")):
            try:
                doc = load_markdown(path)
            except Exception as exc:
                log_error(args.vault, f"stale open-loop load failed for {path}", exc)
                continue
            review_after = doc.frontmatter.get("review_after")
            if not review_after:
                continue
            try:
                if date.today() > date.fromisoformat(str(review_after)):
                    print(f"{path.name}: review_after={review_after}")
            except ValueError:
                continue
        return 0

    if args.command == "loops":
        for path in sorted((args.vault / "open_loops").glob("*.md")):
            print(path.name)
        return 0

    if args.command == "decay":
        print(detect_decay_candidates(vault=args.vault))
        return 0

    if args.command == "review":
        if getattr(args, "review_command", None) == "batch":
            if args.write:
                path = write_batch_review(args.vault)
                print(path)
            else:
                print(generate_batch_review(args.vault))
            return 0
        from .frontmatter import load_markdown
        from .tools.deixis import render_for_display

        drafts = sorted((args.vault / "drafts").glob("*.md"))
        queued_any = False
        for path in drafts:
            try:
                doc = load_markdown(path)
                summary = str(doc.frontmatter.get("summary", ""))
                status = str(doc.frontmatter.get("status", ""))
                pipeline = doc.frontmatter.get("pipeline", {})
                task = pipeline.get("task", "") if isinstance(pipeline, dict) else ""
            except Exception as exc:
                log_error(args.vault, f"review draft load failed for {path}", exc)
                summary = ""
                status = ""
                task = ""
            if status not in {"pending", "needs_revision"}:
                continue
            queued_any = True
            print(f"{path.name} | {status} | {task} | {render_for_display(summary, args.vault)}")
        if not queued_any:
            print("No queued drafts.")
        return 0

    if args.command == "new":
        return _handle_new(args)

    if args.command == "assemble":
        query = " ".join(args.query)
        print(assemble_context(query, domain=args.domain, vault=args.vault))
        return 0

    if args.command == "heuristic":
        result = score_text(" ".join(args.text), db_path=sqlite_path(), vault=args.vault)
        print(json.dumps(result.as_dict(), indent=2))
        return 0

    if args.command == "complete":
        config = load_config()
        llm = LisanLLM(config=config)
        try:
            response = llm.complete(
                " ".join(args.prompt),
                agent=args.agent,
                significance=args.significance,
                provider=args.provider,
                model=args.model,
            )
        except ProviderError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(response.text)
        return 0

    if args.command == "provider":
        config = load_config()
        if args.provider_command == "check":
            result = diagnose_provider(provider=args.provider, model=args.model, config=config)
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=True))
            return 0 if result.status in {"ok", "warning"} else 1

    if args.command == "skills":
        if args.skills_command == "auth":
            import subprocess as _sp
            import sys as _sys

            broker = Path(__file__).resolve().parent.parent / "skills" / "_google_common" / "setup.py"
            if not broker.exists():
                print("No auth broker found for that skill.")
                return 1
            import os as _os

            env = dict(_os.environ)
            try:
                from .config import load_config

                cred_dir = str(((load_config().get("skills") or {}).get("google") or {}).get("credentials_dir") or "")
                if cred_dir:
                    # the app's config is authoritative; the standalone broker
                    # must never resolve a different directory than the skills do
                    env["LISAN_GOOGLE_CREDENTIALS_DIR"] = cred_dir
            except Exception:
                pass
            if args.pasted_url:
                cmd = [_sys.executable, str(broker), "--auth-code", args.pasted_url]
            else:
                r = _sp.run([_sys.executable, str(broker), "--check"], capture_output=True, text=True, env=env)
                print(r.stdout.strip())
                cmd = [_sys.executable, str(broker), "--auth-url"]
            r = _sp.run(cmd, capture_output=True, text=True, env=env)
            print(r.stdout.strip() or r.stderr.strip())
            if not args.pasted_url and r.returncode == 0:
                print("\nOpen that URL, approve access, then run:")
                print("  lisan skills auth", args.name, "'<the full URL your browser lands on>'")
            return r.returncode

        from .tools.skills_cli import (
            install_all,
            install_skill,
            setup_skill,
            skills_status,
            uninstall_skill,
        )
        from .paths import skills_root

        if args.skills_command == "list":
            rows = skills_status()
            if not rows:
                print("No skills found (bundled or installed).")
                return 0
            print(f"Skills directory: {skills_root()}\n")
            for row in rows:
                state = "installed" if row["installed"] else "bundled (not installed)"
                gate = " [requires approval]" if row["requires_approval"] else ""
                print(f"  {row['name']:<22} {state}{gate}")
                if row["description"]:
                    print(f"      {row['description']}")
            print("\nInstall with: lisan skills install <name>   (or --all)")
            return 0
        if args.skills_command == "install":
            try:
                if args.all:
                    written = install_all(force=args.force)
                elif args.name:
                    written = install_skill(args.name, force=args.force)
                else:
                    print("Pass a skill name or --all")
                    return 1
            except (ValueError, FileExistsError) as exc:
                print(f"Error: {exc}")
                return 1
            for path in written:
                print(f"Installed {path}")
            print("Installed skills are available in chat immediately (next `lisan chat` turn).")
            return 0
        if args.skills_command == "uninstall":
            try:
                removed = uninstall_skill(args.name)
            except ValueError as exc:
                print(f"Error: {exc}")
                return 1
            print(f"Removed {removed}")
            return 0
        if args.skills_command == "setup":
            forwarded = [a for a in args.setup_args if a != "--"]
            try:
                return setup_skill(args.name, forwarded)
            except ValueError as exc:
                print(f"Error: {exc}")
                return 1

    if args.command == "telegram":
        if args.telegram_command == "setup":
            from .tools.telegram_bot import run_telegram_setup

            return run_telegram_setup()
        if args.telegram_command == "run":
            from .tools.telegram_bot import run_telegram_bot

            return run_telegram_bot(vault=args.vault, provider=args.provider, model=args.model)
        if args.telegram_command == "install-service":
            from .tools.telegram_bot import install_service

            return install_service(vault=args.vault)
        if args.telegram_command == "uninstall-service":
            from .tools.telegram_bot import uninstall_service

            return uninstall_service()

    if args.command == "scheduler":
        if args.scheduler_command == "run":
            from .tools.scheduler import run_scheduler_loop

            print(f"⚕ Lisan scheduler running (poll ceiling {args.poll:g}s). Ctrl-C to stop.")
            try:
                run_scheduler_loop(
                    vault=args.vault,
                    db_path=args.db_path,
                    provider=args.provider,
                    model=args.model,
                    poll_seconds=args.poll,
                    on_tick=lambda summary: print(
                        f"processed {summary.get('processed_count')} job(s) "
                        f"({summary.get('success_count')} ok, {summary.get('failure_count')} failed)"
                    ),
                )
            except KeyboardInterrupt:
                print("\nStopped.")
            return 0
        if args.scheduler_command == "install-service":
            from .tools.scheduler import install_scheduler_service

            return install_scheduler_service(vault=args.vault)
        if args.scheduler_command == "uninstall-service":
            from .tools.scheduler import uninstall_scheduler_service

            return uninstall_scheduler_service()

    if args.command == "task":
        from .tools.scheduler import cancel_task, format_task_list, list_tasks, schedule_task

        if args.task_command == "add":
            if args.every and args.daily:
                print("✗ Use either --every or --daily, not both.")
                return 1
            every = args.every.removeprefix("every:") if args.every else None
            daily = args.daily.removeprefix("daily@") if args.daily else None
            recurrence = f"every:{every}" if every else (f"daily@{daily}" if daily else None)
            try:
                summary = schedule_task(
                    kind=args.kind,
                    text=args.text,
                    when=args.at,
                    recurrence=recurrence,
                    working_directory=args.working_directory,
                    db_path=args.db_path,
                )
            except ValueError as exc:
                print(f"✗ {exc}")
                return 1
            recur_note = f" (recurring {summary['recurrence']})" if summary.get("recurrence") else ""
            print(f"✓ Scheduled {summary['kind']} for {summary['scheduled_for_local']}{recur_note}")
            print(f"  id: {summary['job_id']}")
            return 0
        if args.task_command == "list":
            print(format_task_list(list_tasks(db_path=args.db_path)))
            return 0
        if args.task_command == "cancel":
            job = cancel_task(args.job_id, db_path=args.db_path)
            if job is None:
                print(f"✗ No such task: {args.job_id}")
                return 1
            print(f"✓ Canceled {args.job_id} (status: {job.get('status')})")
            return 0

    if args.command == "plan":
        from .tools.plans import cancel_plan, create_plan, format_plans, list_plans

        if args.plan_command == "add":
            steps = []
            for raw_step in args.steps:
                kind, _, description = raw_step.partition(":")
                if not description.strip():
                    kind, description = "codex", raw_step
                steps.append({"kind": kind.strip().lower(), "description": description.strip()})
            try:
                summary = create_plan(
                    goal=args.goal, steps=steps,
                    working_directory=args.working_directory, db_path=args.db_path,
                )
            except ValueError as exc:
                print(f"✗ {exc}")
                return 1
            print(f"✓ Plan {summary['plan_id']} created: {summary['goal']} ({summary['steps']} steps)")
            print("  It runs in the background; progress via `lisan plan list` or `lisan self state`.")
            return 0
        if args.plan_command == "list":
            print(format_plans(list_plans(db_path=args.db_path)))
            return 0
        if args.plan_command == "cancel":
            if cancel_plan(args.plan_id, db_path=args.db_path):
                print(f"✓ Canceled {args.plan_id}")
                return 0
            print(f"✗ No active plan {args.plan_id}")
            return 1
        if args.plan_command == "ingest-folder":
            from .tools.plans import build_folder_ingestion_plan

            try:
                summary = build_folder_ingestion_plan(
                    args.path, batch_size=args.batch, limit=args.limit, db_path=args.db_path
                )
            except ValueError as exc:
                print(f"✗ {exc}")
                return 1
            print(f"✓ Plan {summary['plan_id']}: {summary['goal']} ({summary['steps']} steps)")
            print("  Running in the background; results and questions arrive via Telegram.")
            return 0

    if args.command == "browser":
        from .tools.browser import _cdp_alive, browser_action

        if args.browser_command == "status":
            print("running" if _cdp_alive() else "not running")
            return 0
        if args.browser_command == "open":
            r = browser_action("open")
        elif args.browser_command == "goto":
            r = browser_action("goto", url=args.url)
        else:
            r = browser_action("read")
        import json as _json

        print(_json.dumps(r, indent=2, ensure_ascii=True)[:4000])
        return 0 if r.get("ok") else 1

    if args.command == "entities":
        from .tools.entity_merge import dedup_candidates, merge_entities

        vault = vault_root()
        if args.entities_command == "dedup":
            cands = dedup_candidates(vault)
            if not cands:
                print("No near-duplicate entities found.")
                return 0
            for c in cands:
                print(f"[{c['kind']:>12}] absorb '{c['absorb']}' into '{c['keep']}'  ({c['why']})")
            print(f"\n{len(cands)} candidate(s). Merge with: lisan entities merge <source> <target>")
            return 0
        if args.entities_command == "merge":
            result = merge_entities(vault, args.source, args.target, db_path=sqlite_path())
            if result.get("merged"):
                print(f"Merged '{result['source']}' into '{result['target']}' (fragment archived: {result['archived']})")
                return 0
            print(f"Not merged: {result.get('reason')}")
            return 1

    if args.command == "deviations":
        from .config import load_config
        from .tools.deviations import scan_deviations, _self_loops

        vault = vault_root()
        if args.deviations_command == "scan":
            result = scan_deviations(vault, db_path=sqlite_path(), config=load_config())
            print(f"detected={result['detected']} emitted={result['emitted']} satiated={result['satiated']}")
            for loop_id in result.get("emitted_ids") or []:
                print(f"  + {loop_id}")
            return 0
        if args.deviations_command == "list":
            rows = [(p, fm) for p, fm in _self_loops(vault) if str(fm.get("status")) == "active"]
            if not rows:
                print("No active self-originated loops. Nothing aches.")
                return 0
            for _, fm in rows:
                print(f"[{fm.get('deviation_class','?'):>10}] {fm.get('summary','')}")
            return 0

    if args.command == "self" and getattr(args, "self_command", "") == "evaluate":
        from .config import load_config
        from .tools.self_eval import run_self_evaluation

        result = run_self_evaluation(vault_root(), db_path=sqlite_path(), config=load_config())
        print(f"exchanges={result.get('exchanges')} judged={result.get('judged')} "
              f"overall={result.get('overall_mean')} suggestions={len(result.get('suggestions') or [])}")
        for s in result.get("suggestions") or []:
            print(f"  • {s}")
        print(f"report: {result.get('report')}")
        return 0

    if args.command == "self":
        from .tools.self_model import (
            build_capability_manifest,
            ensure_capabilities_primer,
            render_capability_primer,
            render_self_state,
            snapshot_self_state,
        )

        if args.self_command == "manifest":
            manifest = build_capability_manifest()
            print(json.dumps(manifest, indent=2, ensure_ascii=True) if args.as_json else render_capability_primer(manifest))
            return 0
        if args.self_command == "state":
            state = snapshot_self_state(vault=args.vault, db_path=args.db_path)
            print(json.dumps(state, indent=2, ensure_ascii=True) if args.as_json else render_self_state(state))
            return 0
        if args.self_command == "primer":
            path = ensure_capabilities_primer(args.vault, force=args.force)
            print(f"✓ Wrote {path}" if path else "Already current.")
            return 0
        if args.self_command == "extract-voice":
            from .tools.voice_extract import run_extraction

            result = run_extraction(
                args.vault,
                provider=args.provider,
                model=args.model,
                out=args.out,
                min_invariants=args.min_invariants,
                min_conversations=args.min_conversations,
                max_turns=args.max_turns,
            )
            stats = result["stats"]
            print(f"Scanned {stats.get('turns', 0)} agent turns across {stats.get('conversations', 0)} conversations.")
            print(f"Candidates: {len(result['candidates'])} valid, {len(result['rejected'])} rejected by the evidence gate.")
            print(f"Ceremony eligible: {'yes' if result['eligible'] else 'no'}")
            print(f"Artifact: {result['artifact']}")
            return 0
        if args.self_command == "ratify":
            from .frontmatter import load_markdown as _load_md

            artifact_kind = ""
            try:
                artifact_kind = str(_load_md(args.artifact).frontmatter.get("artifact_kind") or "")
            except Exception:
                pass
            if artifact_kind == "beliefs":
                if args.provisional:
                    print("Beliefs have no provisional path — they enter owner-ratified or not at all.", file=sys.stderr)
                    return 1
                from .tools.belief_formation import ratify_beliefs

                created = ratify_beliefs(args.vault, artifact_path=args.artifact)
                for path in created:
                    print(f"✓ Formed belief: {path.name}")
                print(f"✓ Ratified {len(created)} belief(s) from {args.artifact.name}")
                return 0
            from .tools.voice_extract import ratify_voice

            path = ratify_voice(args.vault, artifact_path=args.artifact, provisional=args.provisional)
            print(f"✓ Ratified voice into {path}" + (" (provisional — pending owner review)" if args.provisional else ""))
            return 0
        if args.self_command == "extract-beliefs":
            from .tools.belief_formation import run_belief_extraction

            result = run_belief_extraction(args.vault, out=args.out)
            print(f"Candidates: {result['candidates']}")
            print(f"Artifact: {result['artifact']}")
            return 0
        if args.self_command == "backfill-episodes":
            from .tools.self_episodes import assemble_self_episodes

            result = assemble_self_episodes(args.vault, args.db_path)
            print(f"✓ Wrote {result['written']} self-episode(s).")
            return 0

    if args.command == "sync":
        _bootstrap_runtime(args.vault, ensure_schema=True)
        generate_manifests(args.vault, write=True)
        write_current_brief(args.vault)
        write_batch_review(args.vault)
        report = validate_vault(args.vault)
        counts = rebuild_index(args.vault)
        from .tools.learned_edges import mine_learned_edges
        from .tools.retrospective import sweep_missed_captures

        counts["learned_edges"] = mine_learned_edges()["edges_written"]
        counts["retrospective_enqueued"] = sweep_missed_captures(args.vault)["enqueued"]
        health = generate_health_report(args.vault)
        health_out = args.vault / "reports" / "health-latest.md"
        health_out.parent.mkdir(parents=True, exist_ok=True)
        health_out.write_text(health, encoding="utf-8")
        print(format_report(report))
        print(json.dumps(counts, indent=2))
        return 0 if report.ok else 1

    if args.command == "agent":
        return _handle_agent(args)

    if args.command == "prompts":
        for name in list_prompts():
            print(name)
        return 0

    if args.command == "prompt":
        if args.prompt_command == "show":
            print(load_prompt(args.name))
            return 0

    if args.command == "transcript":
        if args.transcript_command == "append":
            path = append_transcript(
                vault=args.vault,
                conversation_id=args.conversation_id,
                speaker=args.speaker,
                text=" ".join(args.text),
            )
            print(path)
            return 0

    if args.command == "conversation":
        if args.conversation_command == "show":
            state = load_narrative_state(args.vault, args.conversation_id)
            print(json.dumps(state.as_dict(), indent=2))
            return 0
        if args.conversation_command == "history":
            history = conversation_history(args.vault, args.conversation_id)
            print(render_narrative_state(load_narrative_state(args.vault, args.conversation_id)).rstrip())
            print("\n---\n")
            if history:
                for turn in history[-20:]:
                    print(f"{turn['speaker']}: {turn['text']}")
            else:
                print("No turns recorded.")
            return 0
        if args.conversation_command == "reset":
            path = reset_narrative_state(args.vault, args.conversation_id)
            print(path)
            return 0
        if args.conversation_command == "digest":
            path = write_conversation_digest(args.vault, args.conversation_id)
            print(path)
            return 0

    if args.command == "capture":
        try:
            result = capture_text(
                vault=args.vault,
                text=" ".join(args.text),
                conversation_id=args.conversation_id,
                speaker=args.speaker,
                provider=args.provider,
                model=args.model,
                append_response_to_transcript=True,
            )
        except ProviderError as exc:
            # Surface a human-readable message instead of a stack trace. The
            # transcript already has the user's turn (and the dedup will
            # suppress a duplicate on retry), so the user can simply rerun.
            print(
                "Provider returned a partial response. Please try again.\n"
                f"  details: {str(exc)[:200]}",
                file=sys.stderr,
            )
            return 1
        # Default to quiet (only Lisan's spoken response). `--verbose` keeps
        # the full pipeline JSON for debugging; explicit `--quiet` is a no-op
        # but documents intent.
        if args.verbose:
            print(json.dumps(result, indent=2))
        else:
            response = str(result.get("response") or "").strip()
            if response:
                print(response)
            elif result.get("mode") == "skip":
                # Stay silent on skipped turns; print a marker if asked.
                if args.quiet:
                    pass
        return 0

    if args.command == "primer-audit":
        try:
            result = run_primer_audit(vault=args.vault, dry_run=args.dry_run, provider=args.provider, model=args.model)
        except ProviderError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(result)
        return 0

    if args.command == "migrate":
        plan = run_migration(vault=args.vault, dry_run=not args.apply)
        print(json.dumps({"needs_migration": plan.needs_migration, "actions": plan.actions}, indent=2))
        return 0

    if args.command == "state":
        if args.state_command == "update":
            try:
                record = upsert_state(
                    args.vault,
                    args.state_category,
                    args.summary,
                    state_secondary=args.state_secondary,
                    privacy=args.privacy,
                    confidence=args.confidence,
                    confidence_basis=args.confidence_basis,
                    sources=args.source,
                    review_after=args.review_after,
                    ttl_days=args.ttl_days,
                )
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(record.path)
            return 0

    if args.command == "show":
        from .frontmatter import load_markdown

        try:
            doc = load_markdown(args.path)
        except FileNotFoundError:
            print(f"Missing file: {args.path}", file=sys.stderr)
            return 1
        print(json.dumps(doc.frontmatter, indent=2))
        print("\n---\n")
        print(doc.body.rstrip())
        return 0

    if args.command == "evidence":
        if args.evidence_command == "correct":
            try:
                record = new_evidence_correction(
                    args.vault,
                    evidence_record_path=args.path,
                    field_corrected=args.field,
                    original_value=args.original,
                    corrected_value=args.corrected,
                    basis=args.basis,
                    approved_by=args.approved_by,
                )
            except (FileNotFoundError, FileExistsError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(record.path)
            return 0

    if args.command == "edit":
        try:
            path = edit_record(args.path, set_fields=args.set, add_fields=args.add, append_body=args.append_body)
        except (FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(path)
        return 0

    if args.command == "archive":
        if args.archive_command == "loop":
            try:
                path = archive_open_loop(args.path, force=args.force)
            except (FileNotFoundError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(path)
            return 0

    if args.command == "backup":
        if args.backup_command == "create":
            try:
                result = create_backup(
                    vault=args.vault,
                    destination=args.destination,
                    recipient=args.recipient,
                    identity=args.identity,
                    encrypt=args.encrypt,
                )
                write_backup_log(args.vault, result)
                if args.test_restore:
                    result = test_backup(result.archive_path, vault=args.vault, identity=args.identity)
                    write_backup_log(args.vault, result)
            except (FileNotFoundError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(result.archive_path)
            return 0
        if args.backup_command == "test":
            archive = args.archive or latest_backup_path(args.destination)
            if archive is None:
                print("No backup archive found.", file=sys.stderr)
                return 1
            try:
                result = test_backup(archive, vault=args.vault, identity=args.identity)
            except (FileNotFoundError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps({
                "archive_path": str(result.archive_path),
                "encrypted": result.encrypted,
                "restore_tested": result.restore_tested,
                "restore_ok": result.restore_ok,
                "restore_message": result.restore_message,
            }, indent=2))
            return 0
        if args.backup_command == "status":
            print(backup_status(args.vault, destination=args.destination))
            return 0

    if args.command == "entity":
        if args.entity_command == "epoch":
            try:
                path = epoch_entity(args.path, summary=args.summary, disambiguation=args.disambiguation)
            except (FileNotFoundError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(path)
            return 0

    if args.command == "draft":
        if args.draft_command == "review":
            try:
                result = review_draft(args.path, vault=args.vault, provider=args.provider, model=args.model, apply=args.apply)
            except (FileNotFoundError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(result, indent=2))
            return 0
        if args.draft_command == "promote":
            try:
                path = promote_draft_to_episode(args.path, args.vault)
            except (FileNotFoundError, FileExistsError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(path)
            return 0
        if args.draft_command == "promote-backlog":
            from .tools.draft_backlog import promote_backlog

            print(json.dumps(promote_backlog(args.vault, args.db_path), indent=2))
            return 0

    if args.command == "dreamer":
        task = args.dreamer_command.replace("-", "_")
        try:
            out = run_dreamer_task(vault=args.vault, task=task, provider=args.provider, model=args.model)
        except (FileNotFoundError, ValueError, ProviderError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(out)
        return 0

    if args.command == "iip":
        if args.iip_command == "challenges":
            from .tools.interpretation import summarize_iip_log

            print(summarize_iip_log(args.vault, weeks=args.weeks))
            return 0

    if args.command == "frameworks":
        from .tools.decode import list_ratified_frameworks, ratify_framework

        if args.frameworks_command == "list":
            entries = list_ratified_frameworks(args.vault)
            if not entries:
                print("No ratified frameworks on record.")
                return 0
            for entry in entries:
                standing = f"  [{entry['prediction_standing']}]" if entry.get("prediction_standing") else ""
                print(f"{entry['id']}  (adopted {entry['adopted']}){standing}")
                print(f"  {entry['summary'][:160]}")
            return 0
        if args.frameworks_command == "ratify":
            out = ratify_framework(args.vault, args.name, args.summary, source=args.source)
            if not out.get("ok"):
                print(out.get("error"), file=sys.stderr)
                return 1
            print(out["path"])
            return 0

    if args.command == "predictions":
        from .tools.predictions import format_prediction_list, list_predictions, run_prediction_reconcile

        if args.predictions_command == "list":
            print(format_prediction_list(list_predictions(args.vault, include_scored=not args.pending)))
            return 0
        if args.predictions_command == "reconcile":
            try:
                summary = run_prediction_reconcile(vault=args.vault, db_path=args.db_path)
            except (ValueError, ProviderError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(summary, indent=2))
            return 0

    if args.command == "analyst":
        if args.analyst_command == "scan":
            try:
                result = run_analyst_scan(vault=args.vault, provider=args.provider, model=args.model)
            except (FileNotFoundError, ValueError, ProviderError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(result.report_path)
            return 0

    if args.command == "patterns":
        if args.patterns_command == "audit":
            report = audit_patterns(args.vault)
            print(format_pattern_audit(report))
            return 0

    if args.command == "jobs":
        if args.jobs_command == "run":
            result = run_jobs_worker(
                vault=args.vault,
                db_path=args.db_path,
                provider=args.provider,
                model=args.model,
                worker_id=args.worker_id,
                max_jobs=args.max_jobs,
            )
            print(json.dumps(result, indent=2, default=str))
            return 0
        if args.jobs_command == "list":
            jobs = list_jobs(status=args.status, limit=args.limit, db_path=args.db_path)
            print(format_job_list(jobs))
            return 0
        if args.jobs_command == "show":
            job = get_job(args.job_id, db_path=args.db_path)
            if job is None:
                print(f"Missing job: {args.job_id}", file=sys.stderr)
                return 1
            print(json.dumps(job, indent=2, default=str))
            return 0
        if args.jobs_command == "retry":
            job = retry_job(args.job_id, db_path=args.db_path)
            if job is None:
                print(f"Missing job: {args.job_id}", file=sys.stderr)
                return 1
            print(json.dumps(job, indent=2, default=str))
            return 0
        if args.jobs_command == "cancel":
            job = cancel_job(args.job_id, db_path=args.db_path)
            if job is None:
                print(f"Missing job: {args.job_id}", file=sys.stderr)
                return 1
            print(json.dumps(job, indent=2, default=str))
            return 0
        if args.jobs_command == "audit":
            report = audit_jobs(vault=args.vault, db_path=args.db_path)
            print(format_job_audit(report))
            return 0
        if args.jobs_command == "reap-stuck":
            report = reap_stuck_jobs(db_path=args.db_path, timeout_minutes=args.timeout_minutes, retry=not args.fail)
            print(json.dumps(report, indent=2, default=str))
            return 0

    parser.print_help()
    return 1


def _confirm_purge() -> bool:
    warning = (
        "WARNING: this will permanently destroy the active vault, backups, and indices. "
        "Back up any files from memory before it runs."
    )
    print(warning)
    response = input("Type PURGE to continue: ").strip()
    return response == "PURGE"


def _confirm_uninstall(*, keep_vault: bool, install_root: Path, bin_dir: Path) -> bool:
    print("WARNING: this will remove the managed Lisan install, virtualenv, launcher, config, and indices.")
    print(f"Install root: {install_root}")
    print(f"Launcher dir:  {bin_dir}")
    if keep_vault:
        print("The vault will be kept.")
    else:
        print("The vault will also be deleted.")
    response = input("Type UNINSTALL to continue: ").strip()
    return response == "UNINSTALL"


def _resolve_purge_options(args: argparse.Namespace) -> tuple[bool, bool] | None:
    if args.yes:
        return bool(args.preserve_config), bool(args.backup_before)

    if not _confirm_purge():
        return None

    preserve_config = _prompt_yes_no("Preserve config.json?", default=False)
    backup_before = _prompt_yes_no("Create a backup before deletion?", default=True)
    return preserve_config, backup_before


def _prompt_yes_no(question: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _handle_new(args: argparse.Namespace) -> int:
    vault = args.vault
    try:
        if args.new_command == "entity":
            record = new_entity(
                vault,
                args.name,
                subtype=args.subtype,
                domain_primary=args.domain_primary,
                domain_secondary=args.domain_secondary,
                privacy=args.privacy,
                significance=args.significance,
                summary=args.summary,
                canonical_name=args.canonical_name,
                aliases=args.alias,
                disambiguation=args.disambiguation,
                compartments=args.compartment,
                allowed_contexts=args.allowed_context,
                blocked_contexts=args.blocked_context,
                confidence=args.confidence,
                confidence_basis=args.confidence_basis,
                review_after=args.review_after,
            )
        elif args.new_command == "episode":
            record = new_episode(
                vault,
                args.title,
                domain_primary=args.domain_primary,
                domain_secondary=args.domain_secondary,
                privacy=args.privacy,
                significance=args.significance,
                source=args.source,
                summary=args.summary,
                entities=args.entity,
                evidence=args.evidence,
                claims=args.claim,
                links=args.link,
                confidence=args.confidence,
                confidence_basis=args.confidence_basis,
                review_after=args.review_after,
                significance_rationale=args.significance_rationale,
            )
        elif args.new_command == "decision":
            record = new_decision(
                vault,
                args.title,
                domain_primary=args.domain_primary,
                domain_secondary=args.domain_secondary,
                privacy=args.privacy,
                significance=args.significance,
                summary=args.summary,
                links=args.link,
                confidence=args.confidence,
                confidence_basis=args.confidence_basis,
                review_after=args.review_after,
                revisit_after=args.revisit_after,
                revisit_conditions=args.revisit_condition,
                alternatives_considered=args.alternative,
            )
        elif args.new_command == "loop":
            record = new_open_loop(
                vault,
                args.title,
                domain_primary=args.domain_primary,
                domain_secondary=args.domain_secondary,
                privacy=args.privacy,
                significance=args.significance,
                summary=args.summary,
                links=args.link,
                confidence=args.confidence,
                confidence_basis=args.confidence_basis,
                review_after=args.review_after,
                priority=args.priority,
                owner=args.owner,
                next_action=args.next_action,
                blocked_by=args.blocked_by,
            )
        elif args.new_command == "knowledge":
            record = new_knowledge(
                vault,
                args.title,
                category=args.category,
                domain_primary=args.domain_primary,
                domain_secondary=args.domain_secondary,
                privacy=args.privacy,
                significance=args.significance,
                summary=args.summary,
                links=args.link,
                confidence=args.confidence,
                confidence_basis=args.confidence_basis,
                review_after=args.review_after,
            )
        elif args.new_command == "evidence":
            record = new_evidence(
                vault,
                args.title,
                source_type=args.source_type,
                source_uri=args.source_uri,
                artifact_ref=args.artifact_ref,
                artifact_hash=args.artifact_hash,
                timestamp_of_artifact=args.artifact_timestamp,
                actors=args.actor,
                arena=args.arena,
                compartments=args.compartment,
                sensitivity=args.sensitivity,
                reliability=args.reliability,
                privacy=args.privacy,
                significance=args.significance,
                summary=args.summary,
                confidence_basis=args.confidence_basis,
                review_after=args.review_after,
                observed_facts=args.observed_fact,
                verbatim_excerpt=args.verbatim_excerpt,
                linked_claims=args.linked_claim,
                linked_episodes=args.linked_episode,
            )
        elif args.new_command == "claim":
            record = new_claim(
                vault,
                args.claim_text,
                claim_class=args.claim_class,
                owner=args.owner,
                status=args.status,
                confidence=args.confidence,
                supporting_evidence=args.supporting_evidence,
                contradicting_evidence=args.contradicting_evidence,
                linked_patterns=args.linked_pattern,
                first_seen=args.first_seen,
                last_reviewed=args.last_reviewed,
                review_notes=args.review_notes,
                arena=args.arena,
                compartments=args.compartment,
                privacy=args.privacy,
                significance=args.significance,
                summary=args.summary,
            )
        elif args.new_command == "state":
            record = new_state(
                vault,
                args.state_category,
                args.summary,
                state_secondary=args.state_secondary,
                privacy=args.privacy,
                confidence=args.confidence,
                confidence_basis=args.confidence_basis,
                sources=args.source,
                review_after=args.review_after,
                ttl_days=args.ttl_days,
            )
        elif args.new_command == "prediction":
            from .tools.predictions import record_prediction

            out = record_prediction(
                vault,
                args.expectation,
                source=args.source,
                review_after=args.review_after,
                trigger=args.trigger,
                subject=args.subject,
                confidence=args.confidence,
            )
            if not out.get("ok"):
                print(out.get("error"), file=sys.stderr)
                return 1
            print(out["path"])
            return 0
        else:
            raise ValueError(f"Unknown new command: {args.new_command}")
    except (FileExistsError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(record.path)
    return 0


def _handle_agent(args: argparse.Namespace) -> int:
    text = " ".join(args.input)
    agent_map = {
        "advice": AdviceAgent,
        "analyst": AnalystAgent,
        "assembler": AssemblerAgent,
        "dreamer": DreamerAgent,
        "elicitor": ElicitorAgent,
        "interlocutor": InterlocutorAgent,
        "listener": ListenerAgent,
        "router": RouterAgent,
        "skeptic": SkepticAgent,
        "writer": WriterAgent,
    }
    agent_cls = agent_map[args.name]
    prompt_file = None
    if args.name == "writer":
        prompt_file = {
            "episode": "writer_episode_v1",
            "entity": "writer_entity_v1",
            "knowledge": "writer_knowledge_v1",
            "state": "writer_state_v1",
            "decision": "writer_decision_v1",
            "open_loop": "writer_open_loop_v1",
            "questions": "writer_questions_v1",
        }.get(args.task or "episode", "writer_episode_v1")
    elif args.name == "dreamer":
        prompt_file = {
            "compress": "dreamer_compress_v1",
            "primer": "dreamer_primer_v1",
            "contradict": "dreamer_contradict_v1",
            "epoch": "dreamer_epoch_v1",
            "confidence": "dreamer_confidence_v1",
            "overfitting": "dreamer_overfitting_v1",
            "identity_anchor": "dreamer_identity_anchor_v1",
        }.get(args.task or "compress", "dreamer_compress_v1")
    elif args.name == "analyst":
        prompt_file = "analyst_v1"
    elif args.name == "listener":
        prompt_file = "listener_v1"
    elif args.name == "elicitor":
        prompt_file = "elicitor_v1"
    elif args.name == "interlocutor":
        prompt_file = "interlocutor_v1"
    elif args.name == "skeptic":
        prompt_file = "skeptic_v1"
    elif args.name == "assembler":
        prompt_file = "assembler_v1"
    elif args.name == "advice":
        prompt_file = "advice_v1"
    elif args.name == "router":
        prompt_file = "mode_router_v1"

    agent = agent_cls(vault=args.vault, prompt_file=prompt_file)
    if args.dry_run:
        print(agent.prompt())
        print("\n--- INPUT ---\n")
        print(text)
        return 0
    try:
        result = agent.run(
            text,
            significance=args.significance,
            provider=args.provider,
            model=args.model,
            task=args.task,
        )
    except ProviderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result.text)
    return 0
