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
    if not (repo_root() / "config.yaml").exists():
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
    purge.add_argument("--preserve-config", action="store_const", const=True, default=None, help="Keep config.yaml instead of resetting it")
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

    logs = subparsers.add_parser("logs", help="Show recent log entries")
    logs.add_argument("--vault", type=Path, default=vault_root())
    logs.add_argument("--tail", type=int, default=50, metavar="N")

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
    for task_name in ["compress", "primer", "contradict", "confidence", "epoch", "overfitting", "identity-anchor"]:
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "logs":
        from .tools.log import tail_log
        print(tail_log(args.vault, lines=args.tail))
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
            backup_before=backup_before,
            backup_destination=args.backup_destination,
        )
        print(f"Purged vault: {result.vault}")
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

    if args.command == "sync":
        _bootstrap_runtime(args.vault, ensure_schema=True)
        generate_manifests(args.vault, write=True)
        write_current_brief(args.vault)
        write_batch_review(args.vault)
        report = validate_vault(args.vault)
        counts = rebuild_index(args.vault)
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

    if args.command == "dreamer":
        task = args.dreamer_command.replace("-", "_")
        try:
            out = run_dreamer_task(vault=args.vault, task=task, provider=args.provider, model=args.model)
        except (FileNotFoundError, ValueError, ProviderError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(out)
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

    preserve_config = _prompt_yes_no("Preserve config.yaml?", default=False)
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
