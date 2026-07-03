# Lisan

Lisan is a local-first memory system and Python CLI for agentic note-taking, recall, and record keeping.

It turns conversation turns and vault content into durable Markdown records, indexes them locally, and uses deterministic retrieval plus embeddings to bring back relevant context later. The design keeps the data readable, editable, and portable, so the vault stays useful even if the surrounding models or providers change.

At a glance:

- Stores memory as plain Markdown with JSON frontmatter
- Builds a local SQLite index for search, claims, and retrieval logging
- Uses a deterministic-first pipeline before it reaches for an LLM
- Supports semantic retrieval through embeddings
- Keeps provider routing abstracted so local, hosted, and coding-agent backends can be swapped
- Exposes the whole system through a CLI instead of a hidden service

The repository is in an MVP-ready state. It is designed so a future maintainer can work from the repository alone without needing a separate design doc.

## What The Current System Includes

- Deterministic vault validation and schema enforcement
- Markdown records with JSON frontmatter
- SQLite indexing with claim extraction and retrieval logging
- Compartment-aware retrieval with SQL metadata, FTS5/BM25, and embedding-based scoring fused with RRF
- Local coding agent CLI as the default provider
- Provider abstraction for OpenAI, Anthropic, Google, local HTTP, and a coding agent CLI
- Full spec-compliant heuristic gate with vault entity lookup, affect scoring, biographical density, decision/open-loop phrase banks, and durable plan detection
- Listener -> Writer -> Skeptic -> Interlocutor capture pipeline
- Memory type routing: Listener classifies input as episode, decision, open_loop, state, knowledge, or entity; Writer selects the correct specialist prompt automatically
- Open loop fan-out: Writer output `open_loops_to_create` is materialized as immediate vault records (open loops are always capture_now)
- Decision fan-out: Writer output `decisions_to_create` materializes decision records in both extraction and elicitor pipelines
- State update fan-out: Writer output `state_updates` is applied to life-domain state files immediately after each conversation turn
- Entity stub fan-out: Writer output `entities_to_create` creates entity stubs with conversation-sourced summaries
- Direct advice responses for non-memory questions in chat, with vault context loaded so personal recall questions ("how many cats do I have?") are answered from stored records
- First-run onboarding flow: blank primer detection triggers a short Q&A that populates `identity.md` and `operating-style.md`
- Per-turn conversation policy that routes advice vs memory and varies tone by context
- A lightweight thinking indicator in chat when provider calls take noticeable time
- Stateful Elicitor mode with per-conversation narrative state
- Elicitor turn-count hard cap (12 turns with ≥3 established facts forces Writer handoff)
- Transcript completeness: LISAN responses written back to transcript alongside user turns
- Manual record creation for entities, episodes, decisions, open loops, knowledge, evidence, and state
- Draft review and promotion
- Dreamer maintenance workflows
- Conversation inspection, history, digest, and reset commands
- Batch review digest generation
- Local backup creation and restore testing
- Current brief regeneration from active state files
- Confidence decay candidate surfacing via deterministic SQL (`lisan decay`)
- Synthetic contradiction testing in ephemeral context (spec §10.3) — read-only; nothing written to storage
- `/remember` and `/forget` prefix stripping before transcript and agent calls
- `/logs [N]` command in interactive chat
- `/domain [name]` command to override retrieval domain for the current session (legacy `/arena` still works)

The repo is usable as a local memory vault CLI now. Most remaining work is refinement, prompt calibration, and optional automation, not core plumbing.

## Install

Single-command install from GitHub:

```bash
curl -fsSL "https://raw.githubusercontent.com/augustgermar/Lisan/main/install.sh?$(date +%s)" | bash
```

This installer creates an isolated virtualenv under `~/.lisan`, so it works on macOS/Homebrew Python without hitting the PEP 668 "externally managed" error. The timestamp query string bypasses GitHub's raw-file cache, so you get the latest script immediately after a push.

If you already have an activated virtualenv and want a manual install, you can still install from git with pip:

```bash
python3 -m pip install "lisan @ git+https://github.com/augustgermar/Lisan.git"
```

If you want PDF reference ingestion, install the optional PDF extra too:

```bash
python3 -m pip install "lisan[pdf]"
```

Then initialize the vault:

```bash
export LISAN_VAULT="$HOME/.local/share/Lisan/vault"
python3 -m lisan init
```

To remove the managed install later without deleting your vault, run:

```bash
lisan uninstall
```

If you want to remove the install from the installer script instead, you can also run:

```bash
curl -fsSL "https://raw.githubusercontent.com/augustgermar/Lisan/main/install.sh?$(date +%s)" | bash -s -- --uninstall
```

## Uninstall

To remove the managed install while keeping your vault data:

```bash
lisan uninstall
```

If you want to remove the vault too, use:

```bash
lisan uninstall --purge-vault
```

The installer wrapper also supports uninstall mode:

```bash
curl -fsSL "https://raw.githubusercontent.com/augustgermar/Lisan/main/install.sh?$(date +%s)" | bash -s -- --uninstall
```

## Vault Location

The active vault location is resolved like this:

- If `LISAN_VAULT` is set, the app uses that path by default.
- If `LISAN_VAULT` is unset, the app creates and uses a local `lisan-vault/` directory inside the repo on first run.

Recommended setup:

```bash
export LISAN_VAULT="$HOME/.local/share/Lisan/vault"
python3 -m lisan init
```

That keeps your personal memories, transcripts, drafts, and reports outside the Git repository while still letting the code operate on them locally.

## Run It

For a fresh checkout:

```bash
cd /path/to/Lisan
export LISAN_VAULT="$HOME/.local/share/Lisan/vault"
python3 -m lisan init
python3 -m lisan sync
python3 -m lisan chat
```

If you have the shell function installed, `lisan` with no arguments launches the chat loop directly.

Useful direct commands:

```bash
python3 -m lisan validate
python3 -m lisan rebuild-index
python3 -m lisan capture --conversation-id demo "I had an unusual day at work"
python3 -m lisan agent advice "What can I make with tuna, pasta, celery, and mayo?"
python3 -m lisan agent elicitor "I am excited to build this"
```

## Reference Ingest

Reference documents can be ingested as chunked knowledge records that link into the vault graph:

```bash
lisan ingest --reference ~/Documents/sdp-training-manual.pdf
lisan ingest --reference ~/Documents/sdp-docs/ --plan
lisan ingest --reference ~/Documents/sdp-training-manual.pdf --link-entity Maya
lisan ingest --reference ~/Documents/sdp-training-manual.pdf --replace
```

Notes:

- `--reference` switches `lisan ingest` into reference mode instead of artifact mode.
- `--plan` previews the chunks and entity links without writing anything.
- `--link-entity` pre-links the imported chunks to a known entity.
- `--replace` re-ingests the same source document by deleting the prior chunks first.
- `--on-exists abort|replace|merge` makes the re-ingest policy explicit; `merge` is reserved for a later release.
- PDF ingestion requires the optional `pymupdf` extra (`pip install "lisan[pdf]"`).

## Working On The Codebase

Start here when making changes:

1. Read this README.
2. Inspect `lisan/cli.py` for the command surface.
3. Inspect `lisan/tools/` for the actual runtime behavior.
4. Inspect `lisan/agents/` for provider-backed and deterministic agent behavior.
5. Inspect `lisan/providers/` for routing and provider adapters.
6. Inspect `lisan/schemas/` for output contracts.
7. Inspect the active vault directory. If `LISAN_VAULT` is set, use that path. Otherwise create or inspect the local vault created by `lisan init`.

The architecture is intentionally deterministic-first. If a feature can be done with file parsing, JSON, regex, or SQL, do that before adding any new LLM behavior.
Treat `pytest` as the release gate; GitHub Actions runs the suite on pushes and pull requests so `main` stays green.

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

- `lisan-vault/`: local vault directory created on first use when `LISAN_VAULT` is unset
- `lisan-vault/primer/`: identity, operating style, and current brief
- `lisan-vault/state/`: per-domain state files
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
- `embeddings.bin`: semantic embedding store used by retrieval (with a model+dimension header; falls back to deterministic hash vectors when configured or when the embedder is unreachable)

## Record Model

All structured records are markdown files with JSON frontmatter.

Required universal frontmatter fields:

- `id`
- `type`
- `created`
- `updated`
- `status`
- `significance`
- `domain_primary` (mirrors legacy `arena_primary` during migration)
- `domain_secondary` (mirrors legacy `arena_secondary` during migration)
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
- `embeddings.bin`: semantic text embeddings for each record, with a model+dimension header
- `files_fts`: FTS index used for keyword retrieval

### Retrieval Path

`lisan/tools/retrieval.py` is the real retrieval engine.

It does all of the following:

- Infers domain context from the query when not explicitly provided
- Loads primer files
- Reads active state files for the selected domain
- Applies compartment gating before load
- Scores candidates with:
  - domain match
  - type priors
  - keyword overlap
  - FTS hits
  - embedding cosine score
- Logs loaded and rejected records into SQLite

`lisan/tools/assembler.py` is just a thin wrapper around that retrieval path now.

### Embeddings

The vector leg of retrieval uses real semantic embeddings, configured under `retrieval.embeddings` in `config.json`. There are three tiers:

1. **Hash floor (no dependencies).** A deterministic, non-semantic `hash_embedding` baseline that never touches the network. This is the reproducible-CI / byte-stable fallback and the A/B control. Force it with `mode: "hash"`.
2. **FastEmbed in-process (recommended).** Qdrant's [FastEmbed](https://github.com/qdrant/fastembed) — a lightweight ONNX embedder, CPU-only, no PyTorch. It runs *inside* the Lisan process (no server to manage). This is the default `provider`.
3. **External HTTP endpoint.** Any OpenAI-compatible `POST {base_url}/v1/embeddings` server (llama.cpp / LM Studio / Ollama-compatible, or a hosted API). Select with `provider: "local"`. A secondary `sentence-transformers` in-process backend also exists behind `provider: "sentence-transformers"` and a lazy import (torch is *not* in the optional extra).

#### Turning on semantic retrieval

Semantic retrieval with FastEmbed is included in the default install. With the shipped defaults
(`provider: "fastembed"`, `mode: "auto"`), semantic retrieval turns on the moment the `fastembed`
package is importable. A **base `pip install lisan`** now installs FastEmbed too. If the embedder
is still unavailable for some reason, Lisan treats it as unreachable, emits one informational
warning, and the `skip` policy drops the vector leg so SQL + FTS carry retrieval. Nothing crashes
or hangs.

The default model is `BAAI/bge-small-en-v1.5` (**384-dim**). FastEmbed downloads the model weights (~90MB for the default) **once** on first use into the cache directory, then reuses them.

#### Config keys

- `mode`: `auto` (default), `semantic`, or `hash`.
  - `auto` attempts the configured embedder and uses it whenever it answers; if it is unreachable (server down, or the FastEmbed extra not installed) it applies `unreachable_policy`. The embed attempt itself is the reachability probe — no separate ping — and it fast-fails (a refused connection or a missing package does not wait out `timeout_seconds`), so a fresh clone or offline CI run never hangs.
  - `semantic` behaves like `auto` but logs a loud warning when the embedder is unreachable.
  - `hash` uses the deterministic `hash_embedding` fallback only and never touches the network.
- `provider`: `fastembed` (default) | `local` (HTTP endpoint) | `sentence-transformers`.
- `model`: the embedding model. Default `BAAI/bge-small-en-v1.5` for FastEmbed; set this to your server's model when `provider: "local"`.
- `cache_dir`: where FastEmbed stores model weights. `null` (default) resolves to `$FASTEMBED_CACHE_PATH` if set, otherwise `~/.cache/lisan/fastembed` (never the system temp dir).
- `query_prefix` / `passage_prefix`: the query-vs-passage convention. BGE-style models are trained to embed a *query* and a *passage* differently, but FastEmbed's native methods apply no distinction for the default model — so Lisan applies the documented convention explicitly. The defaults prefix queries with `Represent this sentence for searching relevant passages: ` and leave passages unprefixed. Set both to `null` to defer to FastEmbed's native `query_embed`/`passage_embed` methods, or set custom strings for a model with a different convention. Records are embedded with the passage form; queries with the query form.
- `unreachable_policy`: `skip` (default) drops the vector leg and marks affected records `embedding_status='pending'` so they are re-embedded later — it never writes hash vectors into a semantic index. `hash` substitutes deterministic hash vectors instead.
- `dimensions`: a **hint only**. The authoritative dimension is whatever the model actually returns, and that is what is written into the `embeddings.bin` header.
- `base_url` / `api_key_env` / `timeout_seconds`: used by the `local` HTTP endpoint.
- `batch_size`: batch size for indexing passes (also passed to FastEmbed's native batching).

#### Behavior and invariants

Indexing embeds record contents in batched passes. At query time the query is embedded exactly once and `embeddings.bin` is loaded once into an in-memory map. If the live query model's dimension differs from the dimension stored in the index header, the vector leg is skipped (never truncated) and a warning instructs you to rebuild.

**Changing the embedding model, or the `passage_prefix`, requires a full `python3 -m lisan rebuild-index`** — the model changes the dimension and vector space, and the passage prefix changes every stored passage vector. **Changing `query_prefix` does not require a rebuild:** query vectors are computed fresh at query time and compared against the existing (bare) passage vectors, the stored index and its dimension stay valid, and the new query instruction takes effect on the next query. Records captured while the embedder was unavailable stay `pending`; they are re-embedded on the next full rebuild, or incrementally via the `index.embed_pending` job. To restore the exact pre-semantic behavior for A/B comparison, set `mode` to `hash`; to go fully keyword-only, uninstall the extra or leave it uninstalled.

### Compartment Rules

Compartment enforcement is deterministic:

- `allowed_contexts` and `blocked_contexts` are checked in retrieval
- `compartments` are treated as boundaries
- Cross-compartment leakage is logged and rejected
- State files can carry domain-specific compartments

## Capture Pipeline

`capture` is the main ingest flow.

Flow:

1. Append transcript entry.
2. Run Listener heuristic scoring.
3. Route the turn with the heuristic fast path first.
4. Use the coding agent router only when the turn is ambiguous.
5. Assess a conversation policy for route, tone, and turn kind.
6. If skipped, stop.
7. If mode is `elicitor`, run the stateful Elicitor path.
8. Otherwise, assemble context, run Writer, Skeptic, and Interlocutor.
9. Write a draft record.

### Listener

`lisan/agents/listener.py` uses `lisan/tools/heuristic_gate.py` for fast-path scoring and falls back to LLM triage for ambiguous turns.

The heuristic gate scores text using:

- Vault entity lookup (+3 per hit, cap +6): names already in the vault raise the score
- Decision phrases ("I decided", "going forward", etc.): +3
- Open-loop phrases ("I need to", "remind me to", etc.): +3
- High-stakes terms from `primer/high-stakes.yaml` (or `heuristic.high_stakes_terms` in config as a fallback): +4
- Affect terms: +2 base, +1 per additional hit up to +4
- Biographical density (multiple personal-detail facts): +3
- Durable plan phrases: +2
- Pure code formatting: -3
- Factual lookup (single question, no personal stake): -3

High-stakes terms are intentionally vault-local and user-defined. There is no
universal hardcoded topic list in source, because what counts as high-stakes is
personal. Future enhancement: Lisan can learn suggested additions dynamically
from recurring high-significance turns and present them back as edits to
`primer/high-stakes.yaml`.

Listener outputs:

- whether the input is worth remembering
- `skip`, `lightweight`, or `full`
- `elicitor` or `extraction` mode
- `memory_type`: episode, decision, open_loop, state, knowledge, entity, or skip
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

The Writer produces structured memory drafts. The task is selected automatically from the Listener's `memory_type` classification. Supported tasks:

- `episode`
- `decision`
- `open_loop`
- `state`
- `knowledge`
- `entity`
- `questions`

Each task has its own specialist prompt (e.g. `writer_decision_v1`, `writer_open_loop_v1`). Writer output is schema-backed and has deterministic fallback behavior when provider calls are unavailable.

Writer output also drives three fan-out actions applied immediately after each pipeline run:

- `entities_to_create` → entity stub records with conversation-sourced summaries
- `open_loops_to_create` → open loop records (always captured immediately per spec)
- `state_updates` → life-domain state file upserts

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
- Backup runs are logged in `backup.md` at the active vault root
- If `LISAN_VAULT` is set, backup commands operate on that external vault by default

## Current Brief

`primer/current-brief.md` is generated from the current state files in the active vault.

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

- `local` is the default provider for all agents
- No API key is required for local use when the local model server is available

Supported providers:

- `codex`
- `openai`
- `google`
- `openrouter`
- `local`

The `local` provider is configured by default to use a local LLM running on this machine.

## Multi-model routing

Lisan routes each agent's LLM calls to a provider based on the turn's significance
level (`low` / `medium` / `high`, determined by the listener's heuristic score).
This lets you keep mechanical agents like Listener, Router, and Assembler on a
cheap local model while reserving a frontier model for agents that need stronger
reasoning or better language quality, like Writer, Skeptic, and Interlocutor.

Configure tiering in `config.json` under `routing`. Each agent has three slots
(`low`, `medium`, `high`) that point at provider names defined under
`providers`. The default routes everything to `local`; customize it to match
your available models and budget.

For example, a local-first setup with Codex reserved for the judgment-heavy
user-facing agents can look like this:

```json
{
  "routing": {
    "writer": { "low": "local", "medium": "codex", "high": "codex" },
    "skeptic": { "low": "local", "medium": "codex", "high": "codex" },
    "interlocutor": { "low": "local", "medium": "codex", "high": "codex" }
  }
}
```

Token-billed APIs charge per token, not per call. Routing small classification
or assembly tasks to cheap models and saving frontier models for high-judgment
turns reduces cost without giving up quality where it matters.

To switch providers, pass `--provider` on the command you are running, for example:

```bash
python3 -m lisan chat --provider local
python3 -m lisan chat --provider openai
python3 -m lisan agent writer --provider local
```

To change the default provider settings, edit `config.json` in the repo root. The provider settings live under the `providers` key, and routing lives under `routing`.

`config.json` is gitignored so your local routing and endpoints stay private; the app falls back to the built-in defaults when it is absent. Copy the tracked template to create your own:

```bash
cp config.example.json config.json
```

The local provider defaults are:

- Base URL: `http://127.0.0.1:8080/v1/chat/completions`
- Model: unset by default; your local server chooses unless you specify one

Environment variables:

- `CODEX_BIN` for the coding agent binary
- `LISAN_VAULT`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GOOGLE_API_KEY`

## Telegram

Talk to Lisan from Telegram. The bot runs the same capture pipeline as `lisan chat`, so messages are remembered and recalled exactly like the CLI. It uses long-polling (no public URL needed) and only the Python standard library.

### Setup (wizard)

The easiest path is the interactive wizard:

```bash
lisan telegram setup
```

It walks you through creating a bot with [@BotFather](https://t.me/BotFather), validates the token live, then **auto-detects your user id** — just message your new bot once and it captures your id (no @userinfobot needed). It saves the token and allowlist to a `telegram:` block in `config.json` (gitignored, so your token stays local). Then:

```bash
lisan telegram run
```

### Setup (manual)

Prefer environment variables? Skip the wizard and export them instead:

```bash
export LISAN_TELEGRAM_TOKEN="123456:ABC-your-bot-token"
export LISAN_TELEGRAM_ALLOWED="<your-user-id>"   # comma-separated for multiple users
lisan telegram run
```

Only ids in the allowlist are answered; everyone else is refused. Environment variables take precedence over the `config.json` `telegram:` block.

### Always-on (auto-start)

To keep the bot running across reboots without leaving a terminal open, install it as an OS service:

```bash
lisan telegram install-service     # launchd on macOS, systemd --user on Linux
lisan telegram uninstall-service   # stop + remove
```

The service runs `lisan telegram run` against your configured vault, restarts automatically if it crashes, and starts on login. Run only one poller at a time — stop any manual `lisan telegram run` before installing the service (Telegram allows a single long-poller per bot).

### In-chat commands

- `/new` — start a fresh conversation
- `/domain <name>` — pin the retrieval domain (no argument clears it)
- `/logs [N]` — show recent log lines
- `/help` — list commands

## Scheduled Tasks

Lisan can perform tasks at a future time — one-shot or recurring. The schedule lives in the jobs database (a row per task, surviving reboots and upgrades); a small scheduler loop notices due rows within seconds and executes them. Nothing is held in cron or in memory.

Three kinds of task:

- **reminder** — sends you a Telegram message at the time (owner-only: delivery can never leave the allowlist)
- **prompt** — runs a prompt through the normal pipeline at the time and sends you the result
- **codex** — runs a codex task at the time; you approve it once, when scheduling

Schedule from conversation ("remind me tomorrow at 9 to call the dentist" — the `schedule_task` tool handles it) or from the shell:

```bash
lisan task add "call the dentist" --at "2026-07-09 15:00"   # local time
lisan task add "stand up and stretch" --every 2h            # recurring interval
lisan task add "summarize my open loops" --kind prompt --daily 09:00
lisan task list
lisan task cancel <task-id>
```

`--at` accepts `YYYY-MM-DD HH:MM` (local), ISO 8601, `HH:MM` (next such time), `tomorrow HH:MM`, or relative offsets like `+30m` / `+2h`. Recurring tasks re-schedule from completion time, so downtime never produces a pile of missed runs — the series just resumes.

The scheduler runs automatically inside the Telegram service. If you don't use Telegram, run it standalone:

```bash
lisan scheduler run                 # foreground loop (Ctrl-C to stop)
lisan scheduler install-service     # always-on: launchd on macOS, systemd --user on Linux
lisan scheduler uninstall-service
```

On WSL, enable systemd (`/etc/wsl.conf`: `[boot]` `systemd=true`) and use `install-service`, or run `lisan scheduler run` from Windows Task Scheduler via `wsl.exe`. Missed work (machine asleep, WSL suspended) is caught up on the next start — reminders that fire late say so.

## Important Commands

Core checks:

```bash
python3 -m lisan validate
python3 -m lisan manifest
python3 -m lisan rebuild-index
python3 -m lisan health
python3 -m lisan sync
```

## Versioning

Lisan uses date-based build versions going forward. The version number should track the current date in `YY.M.D.N` form, where `N` is the build counter for that day, such as `26.5.27.1`.

Capture and conversation:

```bash
python3 -m lisan capture --conversation-id demo "I had an unusual day at work"
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
python3 -m lisan draft review --path "$LISAN_VAULT/drafts/your-draft-file.md"
python3 -m lisan draft review --path "$LISAN_VAULT/drafts/your-draft-file.md" --apply
python3 -m lisan purge
python3 -m lisan purge --yes
python3 -m lisan purge --yes --preserve-config --backup-before
python3 -m lisan purge --yes --backup-before --backup-destination /tmp/lisan-purge-backups
python3 -m lisan backup status
python3 -m lisan backup create
python3 -m lisan backup test
```

`purge` deletes the active vault, backups, and indices, then recreates the fresh-start seed files. It prints a warning, then asks whether to preserve `config.json`, then asks whether to create a backup before deletion. Pass `--yes` to bypass all prompts for automated testing. Use `--preserve-config`, `--backup-before`, and `--backup-destination` with `--yes` to control the non-interactive behavior.

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
python3 -m lisan agent advice "What can I make with tuna, pasta, celery, and mayo?"
python3 -m lisan prompt show writer_episode_v1
python3 -m lisan agent assembler "Need context for the work domain"
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

The repository does not ship personal vault content. When `LISAN_VAULT` is set, your personal vault stays outside the repo. Generated artifacts may appear under:

- `lisan-vault/drafts/`
- `lisan-vault/reports/`
- `lisan-vault/transcripts/`
- `lisan-vault/transcripts/narrative/`

Those are operational outputs of the app.

## Scope

The remaining work is mainly refinement:

- Prompt calibration for long Elicitor sessions
- Optional automation around review items
- Any UI polish you want on top of the CLI
- Future provider/model changes
