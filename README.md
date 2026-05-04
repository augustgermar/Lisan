# Lisan

Local-first Python CLI framework for the Lisan memory vault.

This repository is currently in an MVP-ready state. The codebase is designed so a future Codex session can work from the repository alone without reading `Draft5.md`.

## Current System State

The working system now includes:

- Deterministic vault validation and schema enforcement
- Markdown records with JSON frontmatter
- SQLite indexing with claim extraction and retrieval logging
- Compartment-aware retrieval with keyword, FTS, and embedding-based scoring
- Local Codex CLI as the default provider
- Provider abstraction for OpenAI, Anthropic, Google, local HTTP, and Codex CLI
- Listener -> Writer -> Skeptic -> Interlocutor capture pipeline
- Stateful Elicitor mode with per-conversation narrative state
- Manual record creation for entities, episodes, decisions, open loops, knowledge, evidence, and state
- Draft review and promotion
- Dreamer maintenance workflows
- Conversation inspection, history, digest, and reset commands
- Batch review digest generation
- Local backup creation and restore testing
- Current brief regeneration from active state files

The repo is usable as a local memory vault CLI now. Most remaining work is refinement, prompt calibration, and optional automation, not core plumbing.

## If You Are A Future Codex Session

Start here instead of the draft spec:

1. Read this README.
2. Inspect `lisan/cli.py` for the command surface.
3. Inspect `lisan/tools/` for the actual runtime behavior.
4. Inspect `lisan/agents/` for provider-backed and deterministic agent behavior.
5. Inspect `lisan/providers/` for routing and provider adapters.
6. Inspect `lisan/schemas/` for output contracts.
7. Inspect `lisan-vault/` to understand the current seed vault contents and generated artifacts.

The architecture is intentionally deterministic-first. If a feature can be done with file parsing, JSON, regex, or SQL, do that before adding any new LLM behavior.

## Repository Layout

### Core Code

- `lisan/cli.py`: top-level CLI command router
- `lisan/config.py`: config loading and defaults
- `lisan/paths.py`: repo/vault path helpers and directory layout
- `lisan/providers/`: provider abstraction and provider adapters
- `lisan/agents/`: agent classes and fallback behavior
- `lisan/schemas/`: JSON schemas for agent outputs and record validation
- `lisan/tools/`: deterministic workflows, retrieval, capture, backup, review, index rebuild, etc.
- `lisan/frontmatter.py`: JSON frontmatter parser/writer for markdown records
- `lisan/utils.py`: shared helpers like slugging, hashing, and date formatting

### Vault And Generated Artifacts

- `lisan-vault/`: user vault content
- `lisan-vault/primer/`: identity, operating style, and current brief
- `lisan-vault/state/`: per-arena state files
- `lisan-vault/entities/`: entity records
- `lisan-vault/episodes/`: episode records
- `lisan-vault/knowledge/`: durable knowledge records
- `lisan-vault/evidence/`: evidence records, artifacts, and corrections
- `lisan-vault/decisions/`: decision records
- `lisan-vault/open_loops/`: open loop records
- `lisan-vault/drafts/`: draft records queued for review
- `lisan-vault/transcripts/`: append-only transcripts
- `lisan-vault/transcripts/narrative/`: per-conversation Elicitor state
- `lisan-vault/reports/`: health, batch review, conversation digests, Dreamer reports
- `lisan-vault/manifests/`: derived markdown manifests
- `lisan-vault/backup.md`: backup policy plus backup run log
- `lisan.sqlite`: SQLite index
- `embeddings.bin`: deterministic embedding store used by retrieval

## Record Model

All structured records are markdown files with JSON frontmatter.

Required universal frontmatter fields:

- `id`
- `type`
- `created`
- `updated`
- `status`
- `significance`
- `arena_primary`
- `arena_secondary`
- `privacy`
- `compartments`
- `allowed_contexts`
- `blocked_contexts`
- `summary`
- `links`
- `confidence`
- `confidence_basis`
- `last_confirmed`
- `review_after`

Supported record types:

- `entity`
- `episode`
- `knowledge`
- `evidence`
- `state`
- `decision`
- `open_loop`
- `report`
- `contradiction_log`

The validator enforces field presence, enum values, frontmatter/body consistency, and episode section requirements.

## Retrieval And Indexing

### Index Build

`python3 -m lisan rebuild-index` and `python3 -m lisan sync` rebuild:

- `files` table: one row per structured record
- `links` table: record relationships from frontmatter
- `claims` table: extracted from episode claim tables
- `entity_aliases` table: entity aliases
- `entity_epochs` table: entity history and archive snapshots
- `retrieval_log` table: every retrieval call is logged
- `llm_call_log` table: provider call audit trail
- `embeddings.bin`: deterministic text embeddings for each record
- `files_fts`: FTS index used for keyword retrieval

### Retrieval Path

`lisan/tools/retrieval.py` is the real retrieval engine.

It does all of the following:

- Infers arena context from the query when not explicitly provided
- Loads primer files
- Reads active state files for the selected arena
- Applies compartment gating before load
- Scores candidates with:
  - arena match
  - type priors
  - keyword overlap
  - FTS hits
  - embedding cosine score
- Logs loaded and rejected records into SQLite

`lisan/tools/assembler.py` is just a thin wrapper around that retrieval path now.

### Compartment Rules

Compartment enforcement is deterministic:

- `allowed_contexts` and `blocked_contexts` are checked in retrieval
- `compartments` are treated as boundaries
- Cross-compartment leakage is logged and rejected
- State files can carry arena-specific compartments

## Capture Pipeline

`capture` is the main ingest flow.

Flow:

1. Append transcript entry.
2. Run Listener heuristic scoring.
3. If skipped, stop.
4. If mode is `elicitor`, run the stateful Elicitor path.
5. Otherwise, assemble context, run Writer, Skeptic, and Interlocutor.
6. Write a draft record.

### Listener

`lisan/agents/listener.py` is deterministic and uses `lisan/tools/heuristic_gate.py`.

It outputs:

- whether the input is worth remembering
- `skip`, `lightweight`, or `full`
- `elicitor` or `extraction` mode
- reasons and scores

### Elicitor

Elicitor mode is stateful.

Behavior:

- Persists per-conversation narrative state in `lisan-vault/transcripts/narrative/<conversation>.json`
- Uses transcript history plus assembled context
- Produces a response and updated narrative state
- Closes a story on topic shifts or closure cues
- Can emit a draft when the conversation resolves

Conversation commands:

- `lisan conversation show`
- `lisan conversation history`
- `lisan conversation digest`
- `lisan conversation reset`

### Writer

The Writer produces structured memory drafts and supports these tasks:

- `episode`
- `decision`
- `open_loop`
- `state`
- `knowledge`
- `entity`
- `questions`

Writer output is schema-backed and has deterministic fallback behavior when provider calls are unavailable.

### Skeptic

Skeptic reviews drafts for:

- uncertainty
- interpretation drift
- placeholders
- high-risk material

### Interlocutor

Interlocutor handles clarification and review questions. It is used in draft review and in the capture pipeline after Writer/Skeptic.

## Dreamer And Maintenance

Dreamer is the long-horizon maintenance agent.

Tasks supported:

- `compress`
- `primer`
- `contradict`
- `confidence`
- `epoch`
- `overfitting`
- `identity-anchor`

The Dreamer output path writes reports and, for contradictions, a `contradiction_log`.

Other maintenance commands:

- `lisan primer-audit`
- `lisan review batch`
- `lisan health`
- `lisan sync`

## Batch Review

`lisan review batch` generates a consolidated review digest for:

- stale state files
- due drafts
- due open loops
- unresolved/disputed claims
- active conversation state
- stale backup logs

`lisan sync` regenerates the batch review digest automatically.

## Backup Workflow

The backup flow is local and deterministic.

Commands:

- `lisan backup status`
- `lisan backup create`
- `lisan backup test`

Behavior:

- Backup archives include the vault plus the SQLite/embedding/config artifacts
- The archive is staged to avoid concurrent write corruption
- `backup create --test-restore` restores into a temp directory and validates the restored copy
- If `age` is configured and `LISAN_BACKUP_RECIPIENT` is set, encrypted backups are supported
- Backup runs are logged in `lisan-vault/backup.md`

## Current Brief

`primer/current-brief.md` is generated from the current state files.

`lisan sync` refreshes it automatically.

The brief is a volatile briefing document, not a manually maintained note.

## Prompt And Schema Contracts

Prompts live in `prompts/` and are versioned.

Schemas live in `lisan/schemas/` and are used by:

- provider requests when a schema is available
- agent fallback validation
- vault record validation

This repo treats prompt files and schemas as part of the interface contract.

## Provider Routing

Default routing:

- `codex` is the default provider for all agents
- No API key is required for local use when the Codex CLI is available

Supported providers:

- `codex`
- `openai`
- `anthropic`
- `google`
- `local`

Environment variables:

- `CODEX_BIN`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GOOGLE_API_KEY`
- `LISAN_LOCAL_MODEL_URL`
- `LISAN_LOCAL_MODEL`

## Important Commands

Core checks:

```bash
python3 -m lisan validate
python3 -m lisan manifest
python3 -m lisan rebuild-index
python3 -m lisan health
python3 -m lisan sync
```

Capture and conversation:

```bash
python3 -m lisan capture --conversation-id demo "I had a weird day at work"
python3 -m lisan conversation show --conversation-id demo
python3 -m lisan conversation history --conversation-id demo
python3 -m lisan conversation digest --conversation-id demo
python3 -m lisan conversation reset --conversation-id demo
```

Review and maintenance:

```bash
python3 -m lisan review
python3 -m lisan review batch
python3 -m lisan review batch --write
python3 -m lisan draft review --path lisan-vault/drafts/your-draft-file.md
python3 -m lisan draft review --path lisan-vault/drafts/your-draft-file.md --apply
python3 -m lisan backup status
python3 -m lisan backup create
python3 -m lisan backup test
```

Manual record creation:

```bash
python3 -m lisan new entity "Ada Lovelace"
python3 -m lisan new episode "First Meeting"
python3 -m lisan new decision "Use CLI"
python3 -m lisan new loop "Follow up"
python3 -m lisan new knowledge "Vault Architecture"
python3 -m lisan new evidence "Screenshot 1" --artifact-text "sample artifact"
python3 -m lisan new state work "Work is currently in setup mode."
```

Provider and prompt inspection:

```bash
python3 -m lisan prompts
python3 -m lisan prompt show writer_episode_v1
python3 -m lisan agent assembler "Need context for the work arena"
python3 -m lisan agent listener "forget this"
python3 -m lisan agent writer --task questions --dry-run "What should I ask next?"
python3 -m lisan agent dreamer --task primer --dry-run "Build the yearly primer"
```

## How To Modify The Codebase

If you are making a change, the likely file targets are:

- `lisan/cli.py` for command surfaces
- `lisan/tools/` for deterministic workflows
- `lisan/agents/` for agent behavior or fallback outputs
- `lisan/providers/` for provider routing or transport
- `lisan/schemas/` for contract changes
- `lisan/tools/validator.py` for vault rules
- `README.md` for user-facing usage and operational handoff

Typical implementation pattern:

1. Put deterministic logic in `lisan/tools/`.
2. Add or update a schema if an agent output shape changed.
3. Update the CLI to expose the workflow.
4. Run `python3 -m lisan sync`.
5. Verify the generated artifacts and logs.

## Seed Vault Notes

The checked-in vault content is intentionally small and acts as a seed vault. Generated artifacts may appear under:

- `lisan-vault/drafts/`
- `lisan-vault/reports/`
- `lisan-vault/transcripts/`
- `lisan-vault/transcripts/narrative/`

Those are operational outputs of the app.

## Scope

The remaining work is mainly refinement:

- prompt calibration for long Elicitor sessions
- optional automation around review items
- any UI polish you want on top of the CLI
- future provider/model changes
