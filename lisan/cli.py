from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import load_config, save_default_config
from .agents import AdviceAgent, AssemblerAgent, DreamerAgent, ElicitorAgent, InterlocutorAgent, ListenerAgent, RouterAgent, SkepticAgent, WriterAgent
from .paths import ensure_repo_layout, repo_root, sqlite_path, vault_root, write_seed_files
from .providers.base import LisanLLM, ProviderError
from .prompts import list_prompts, load_prompt
from .tools.assembler import assemble_context
from .tools.chat import run_chat
from .tools.capture import capture_text
from .tools.current_brief import write_current_brief
from .tools.conversation_digest import write_conversation_digest
from .tools.dreamer_ops import run_dreamer_task
from .tools.draft_review import review_draft
from .tools.archive import archive_open_loop
from .tools.backup import backup_status, create_backup, latest_backup_path, test_backup, write_backup_log
from .tools.batch_review import generate_batch_review, write_batch_review
from .tools.epochs import epoch_entity
from .tools.drafts import promote_draft_to_episode
from .tools.editor import edit_record
from .tools.health_report import generate_health_report
from .tools.heuristic_gate import score_text
from .tools.confidence_decay import detect_decay_candidates
from .tools.manifest_gen import generate_manifests
from .tools.migrator import run_migration
from .tools.primer_audit import run_primer_audit
from .tools.record_factory import new_decision, new_evidence, new_evidence_correction, new_entity, new_episode, new_knowledge, new_open_loop, new_state, upsert_state
from .tools.rebuild_index import rebuild_index
from .tools.narrative_state import conversation_history, load_narrative_state, render_narrative_state, reset_narrative_state
from .tools.transcripts import append_transcript
from .tools.validator import format_report, validate_vault


def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    parser = argparse.ArgumentParser(prog="lisan")
    parser.add_argument("--version", action="version", version=f"lisan {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create the default vault layout and config")

    chat = subparsers.add_parser("chat", help="Start an interactive chat session")
    chat.add_argument("--vault", type=Path, default=vault_root())
    chat.add_argument("--conversation-id", default=None)
    chat.add_argument("--provider", default=None)
    chat.add_argument("--model", default=None)

    state = subparsers.add_parser("state", help="Inspect or update state files")
    state_subparsers = state.add_subparsers(dest="state_command", required=True)
    state_update = state_subparsers.add_parser("update", help="Overwrite a current state file")
    state_update.add_argument("--vault", type=Path, default=vault_root())
    state_update.add_argument("arena_primary", choices=["physical", "environmental", "financial", "relational", "work", "status", "appearance", "competence", "social_presence", "desirability"])
    state_update.add_argument("summary")
    state_update.add_argument("--arena-secondary", action="append", default=[])
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
    new_entity_cmd.add_argument("--arena-primary", default="cross_arena")
    new_entity_cmd.add_argument("--arena-secondary", action="append", default=[])
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
    new_episode_cmd.add_argument("--arena-primary", default="cross_arena")
    new_episode_cmd.add_argument("--arena-secondary", action="append", default=[])
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
    new_decision_cmd.add_argument("--arena-primary", default="cross_arena")
    new_decision_cmd.add_argument("--arena-secondary", action="append", default=[])
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
    new_loop_cmd.add_argument("--arena-primary", default="cross_arena")
    new_loop_cmd.add_argument("--arena-secondary", action="append", default=[])
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
    new_knowledge_cmd.add_argument("--arena-primary", default="cross_arena")
    new_knowledge_cmd.add_argument("--arena-secondary", action="append", default=[])
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
    new_evidence_cmd.add_argument("--subtype", default="document")
    new_evidence_cmd.add_argument("--arena-primary", default="cross_arena")
    new_evidence_cmd.add_argument("--arena-secondary", action="append", default=[])
    new_evidence_cmd.add_argument("--privacy", default="personal")
    new_evidence_cmd.add_argument("--significance", default="low")
    new_evidence_cmd.add_argument("--summary", default=None)
    new_evidence_cmd.add_argument("--supports", action="append", default=[])
    new_evidence_cmd.add_argument("--correction", action="append", default=[])
    new_evidence_cmd.add_argument("--link", action="append", default=[])
    new_evidence_cmd.add_argument("--confidence", default="high")
    new_evidence_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_evidence_cmd.add_argument("--review-after", default=None)
    new_evidence_cmd.add_argument("--artifact-text", default=None)

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
    new_state_cmd.add_argument("arena_primary", choices=["physical", "environmental", "financial", "relational", "work", "status", "appearance", "competence", "social_presence", "desirability"])
    new_state_cmd.add_argument("summary")
    new_state_cmd.add_argument("--arena-secondary", action="append", default=[])
    new_state_cmd.add_argument("--privacy", default="personal")
    new_state_cmd.add_argument("--confidence", default="low")
    new_state_cmd.add_argument("--confidence-basis", default="User-authored placeholder")
    new_state_cmd.add_argument("--source", action="append", default=[])
    new_state_cmd.add_argument("--review-after", default=None)
    new_state_cmd.add_argument("--ttl-days", type=int, default=None)

    assemble = subparsers.add_parser("assemble", help="Assemble context for a query")
    assemble.add_argument("query", nargs="+")
    assemble.add_argument("--arena", default=None)
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
    agent.add_argument("name", choices=["advice", "assembler", "dreamer", "elicitor", "interlocutor", "listener", "router", "skeptic", "writer"])
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

    dreamer = subparsers.add_parser("dreamer", help="Run Dreamer maintenance analyses")
    dreamer_subparsers = dreamer.add_subparsers(dest="dreamer_command", required=True)
    for task_name in ["compress", "primer", "contradict", "confidence", "epoch", "overfitting", "identity-anchor"]:
        dreamer_task = dreamer_subparsers.add_parser(task_name, help=f"Run the Dreamer {task_name} task")
        dreamer_task.add_argument("--vault", type=Path, default=vault_root())
        dreamer_task.add_argument("--provider", default=None)
        dreamer_task.add_argument("--model", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "logs":
        from .tools.log import tail_log
        print(tail_log(args.vault, lines=args.tail))
        return 0

    if args.command == "chat":
        return run_chat(
            vault=args.vault,
            conversation_id=args.conversation_id,
            provider=args.provider,
            model=args.model,
        )

    if args.command == "init":
        ensure_repo_layout()
        if not (repo_root() / "config.yaml").exists():
            save_default_config()
        vault = vault_root()
        seeded = write_seed_files(vault)
        print(f"Lisan workspace initialized at {vault}.")
        if seeded:
            print("\nSeed files created (fill these in before first use):")
            for f in seeded:
                print(f"  {f}")
        if not os.environ.get("LISAN_VAULT"):
            print(
                "\nWARNING: LISAN_VAULT is not set.\n"
                "Personal memories will be written to the repo-local vault and may be\n"
                "accidentally committed to git. Set LISAN_VAULT to an external path\n"
                "before capturing real data:\n\n"
                '  export LISAN_VAULT="$HOME/Library/Application Support/Lisan/vault"\n'
            )
        return 0

    if args.command == "validate":
        report = validate_vault(args.vault)
        print(format_report(report))
        return 0 if report.ok else 1

    if args.command == "manifest":
        manifests = generate_manifests(args.vault, write=not args.no_write)
        print("\n".join(sorted(manifests)))
        return 0

    if args.command == "rebuild-index":
        counts = rebuild_index(args.vault)
        print(json.dumps(counts, indent=2))
        return 0

    if args.command == "health":
        report = generate_health_report(args.vault)
        out = args.vault / "reports" / "health-latest.md"
        out.write_text(report, encoding="utf-8")
        print(out)
        return 0

    if args.command == "stale":
        from datetime import date

        from .frontmatter import load_markdown

        for path in sorted((args.vault / "state").glob("*.md")):
            try:
                doc = load_markdown(path)
            except Exception:
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
            except Exception:
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

        drafts = sorted((args.vault / "drafts").glob("*.md"))
        if not drafts:
            print("No queued drafts.")
            return 0
        for path in drafts:
            try:
                doc = load_markdown(path)
                summary = str(doc.frontmatter.get("summary", ""))
                status = str(doc.frontmatter.get("status", ""))
                pipeline = doc.frontmatter.get("pipeline", {})
                task = pipeline.get("task", "") if isinstance(pipeline, dict) else ""
            except Exception:
                summary = ""
                status = ""
                task = ""
            print(f"{path.name} | {status} | {task} | {summary}")
        return 0

    if args.command == "new":
        return _handle_new(args)

    if args.command == "assemble":
        query = " ".join(args.query)
        print(assemble_context(query, arena=args.arena, vault=args.vault))
        return 0

    if args.command == "heuristic":
        result = score_text(" ".join(args.text), db_path=sqlite_path())
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

    if args.command == "sync":
        generate_manifests(args.vault, write=True)
        write_current_brief(args.vault)
        write_batch_review(args.vault)
        report = validate_vault(args.vault)
        counts = rebuild_index(args.vault)
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
        result = capture_text(
            vault=args.vault,
            text=" ".join(args.text),
            conversation_id=args.conversation_id,
            speaker=args.speaker,
            provider=args.provider,
            model=args.model,
        )
        print(json.dumps(result, indent=2))
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
                    args.arena_primary,
                    args.summary,
                    arena_secondary=args.arena_secondary,
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

    parser.print_help()
    return 1


def _handle_new(args: argparse.Namespace) -> int:
    vault = args.vault
    try:
        if args.new_command == "entity":
            record = new_entity(
                vault,
                args.name,
                subtype=args.subtype,
                arena_primary=args.arena_primary,
                arena_secondary=args.arena_secondary,
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
                arena_primary=args.arena_primary,
                arena_secondary=args.arena_secondary,
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
                arena_primary=args.arena_primary,
                arena_secondary=args.arena_secondary,
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
                arena_primary=args.arena_primary,
                arena_secondary=args.arena_secondary,
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
                arena_primary=args.arena_primary,
                arena_secondary=args.arena_secondary,
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
                subtype=args.subtype,
                arena_primary=args.arena_primary,
                arena_secondary=args.arena_secondary,
                privacy=args.privacy,
                significance=args.significance,
                summary=args.summary,
                supports=args.supports,
                corrections=args.correction,
                links=args.link,
                confidence=args.confidence,
                confidence_basis=args.confidence_basis,
                review_after=args.review_after,
                artifact_text=args.artifact_text,
            )
        elif args.new_command == "state":
            record = new_state(
                vault,
                args.arena_primary,
                args.summary,
                arena_secondary=args.arena_secondary,
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
