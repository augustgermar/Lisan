# Lisan

**A story-based, local-first memory vault for personal AI.**

Lisan gives an AI agent durable, lifelong memory of *you* — not as a pile of embedded chunks, but as a curated library of stories, decisions, open questions, and evolving state. It runs entirely on your machine, stores everything as plain Markdown you can read and edit, and is built to keep working for decades.

> Memory is not storage. Memory is narrative.
>
> Human beings don't remember facts — they remember stories. A well-told story preserves more meaning per token than the equivalent pile of raw facts, because causal structure, emotional weight, and significance are already encoded in the narrative form. Lisan treats the *story* as the unit of memory, and the agent's primary job as **authorship**, not retrieval.

---

## Why Lisan exists

Every LLM conversation starts from a blank slate. The model doesn't know who you are, what matters to you, what you decided last month, or what happened last year. The context window is finite; the life it needs to serve is not.

Lisan is the mechanism that closes that gap. It captures what's worth remembering from your conversations, writes it into well-structured narrative records, and assembles the *most relevant* context for any future conversation — so a personal agent can have genuine continuity across years.

It is deliberately more than a database. It's a personal ontology, an autobiographical historian, a context assembler, and an editorial governance layer that actively guards against the real failure modes of lifelong memory:

- overfitting to a single interpretation of your life
- old emotional conclusions persisting after circumstances change
- legal/medical/relational claims getting canonized too early
- reinforcing a story because it's *coherent*, not because it's *true*

## Design principles

- **Local-first.** No cloud, no remote, no account. Your vault is a folder of Markdown files on your disk plus a local SQLite index. Encrypted backups are at your discretion.
- **Deterministic-first.** Every function is evaluated against a hierarchy — plain code → SQL → regex/parser → small local model → frontier LLM — and LLM calls are reserved for what genuinely needs narrative comprehension (writing, challenging, fact-checking, consolidating). Everything else is code, so it's fast, reproducible, and auditable.
- **Provider-agnostic.** Models will change constantly over a lifetime. Every agent call goes through a provider abstraction (local server, OpenAI, Anthropic, Google, OpenRouter, or a coding-agent CLI) and is logged with provider, model, prompt version, and input/output hashes.
- **Human-readable & durable.** Records are Markdown with JSON frontmatter. You can read, grep, edit, and back them up with ordinary tools. The format is designed to still make sense in twenty years.
- **Plain-text vault, code-defined contracts.** Prompts (`prompts/`) and JSON schemas (`lisan/schemas/`) are treated as part of the interface contract and are versioned.

## How it works

Lisan ingests your conversation turns through a multi-agent pipeline. Cheap deterministic checks run first; LLM-backed agents are invoked only when a turn is actually worth the cost.

```
turn → Listener → (Router) → Policy → Elicitor          (stateful, conversational)
                                    └→ Writer → Skeptic → Interlocutor → draft
```

| Agent | Role |
| --- | --- |
| **Listener** | Scores each turn with a deterministic heuristic gate (entity hits, decision/open-loop phrases, affect, risk keywords, biographical density). Decides *skip / lightweight / full*, picks a memory type, and only falls back to an LLM when the turn is ambiguous. |
| **Router** | Resolves ambiguous turns between *elicitor* and *extraction* modes. |
| **Elicitor** | A stateful conversational mode that draws stories out over multiple turns, tracking per-conversation narrative state and emitting a draft when a story resolves. |
| **Writer** | Produces structured memory drafts using a type-specific specialist prompt (episode, decision, open loop, state, knowledge, entity, questions). Schema-backed, with deterministic fallbacks. |
| **Skeptic** | Reviews drafts for uncertainty, interpretation drift, placeholders, and high-risk material. |
| **Interlocutor** | Handles clarification and review questions during capture and draft review. |
| **Dreamer** | Long-horizon maintenance: compression, primer regeneration, contradiction detection, confidence decay, entity epochs, overfitting and identity-anchor audits. |
| **Analyst** | Longitudinal scan for recurring pattern hypotheses across the vault. |

Writer output also drives immediate **fan-out**: new open loops, entity stubs, and life-domain state updates are materialized into the vault right after a turn.

### The vault

Everything lives in your vault directory as Markdown records with JSON frontmatter:

```
primer/        identity, operating style, current brief (the agent's self-model)
state/         per-domain "what's true right now" files
domains/       domain definitions
entities/      people, places, things
episodes/      narrative events
knowledge/     durable facts
evidence/      artifacts, claims, corrections
decisions/     decisions made
open_loops/    unresolved threads
drafts/        records queued for review
transcripts/   append-only conversation log (+ narrative/ Elicitor state)
reports/       health, batch-review, digests, Dreamer output
```

Alongside the vault: `lisan.sqlite` (the index) and `embeddings.bin` (semantic vectors).

Every structured record carries universal frontmatter (`id`, `type`, `created`/`updated`, `status`, `significance`, `domain_primary`/`secondary`, `privacy`, `compartments`, `allowed_contexts`/`blocked_contexts`, `summary`, `links`, `confidence`, `confidence_basis`, `last_confirmed`, `review_after`). A deterministic validator enforces field presence, enum values, frontmatter/body consistency, and section requirements.

### Retrieval

Retrieval (`lisan/tools/retrieval.py`) infers the relevant domain, loads primer and state, enforces privacy **compartments before load**, then scores candidates across four legs — SQL metadata, keyword overlap, FTS5/BM25, and embedding cosine — and fuses them with Reciprocal Rank Fusion (RRF). Every loaded and rejected record is logged to SQLite.

**Semantic retrieval is optional and zero-config to enable.** Installing the extra *is* the activation:

```bash
pip install "lisan[embeddings]"
```

This pulls in [FastEmbed](https://github.com/qdrant/fastembed) (Qdrant's lightweight ONNX embedder — CPU-only, in-process, no PyTorch, no server). With the shipped defaults, semantic retrieval turns on the moment `fastembed` is importable. Without it, Lisan runs clean keyword-only retrieval (SQL + FTS) — nothing crashes or hangs. A deterministic hash-vector floor is also available (`mode: "hash"`) for byte-stable CI and A/B control.

## Install

One-line install (clones the repo into `~/.lisan`, sets up an isolated venv, and drops a `lisan` launcher on your PATH):

```bash
curl -fsSL https://raw.githubusercontent.com/augustgermar/Lisan/main/install.sh | bash
```

Add semantic retrieval by exporting `LISAN_EMBEDDINGS=1` before running the installer.

Or install from a clone (requires Python ≥ 3.11; the core runtime is **pure standard library**):

```bash
git clone https://github.com/augustgermar/Lisan.git
cd Lisan
pip install -e .            # or: pip install -e ".[embeddings]"
```

## Quick start

```bash
# Keep your personal vault outside the repo
export LISAN_VAULT="$HOME/Library/Application Support/Lisan/vault"

lisan init        # create the vault layout and config
lisan sync        # generate manifests, validate, build the index
lisan chat        # talk to Lisan
```

On first run, a blank primer triggers a short onboarding Q&A that populates your `identity.md` and `operating-style.md`. From then on, chat captures what's worth remembering and recalls it automatically — ask *"how many cats do I have?"* and it answers from your stored records; ask *"what can I make with tuna and pasta?"* and it just gives advice.

If `LISAN_VAULT` is unset, Lisan creates a local `lisan-vault/` inside the repo on first run.

### In-chat commands

- `/remember …` / `/forget …` — force or suppress capture for a turn
- `/domain <name>` — pin the retrieval domain for the session (no arg clears it)
- `/logs [N]` — show recent log lines

## Talk to Lisan from Telegram

The same capture pipeline runs over a Telegram bot — messages are remembered and recalled exactly like the CLI. It uses long-polling (no public URL) and only the standard library.

```bash
lisan telegram setup            # interactive wizard: create bot, validate token, auto-detect your user id
lisan telegram run              # start the long-poll bot
lisan telegram install-service  # optional: always-on launchd/systemd service
```

Only user ids on your allowlist are answered. Run only one poller per bot at a time.

## Providers

The default provider is `local` — a local LLM server, so no API key is required for local use. Supported providers: `local`, `openai`, `anthropic`, `google`, `openrouter`, and a coding-agent CLI.

```bash
lisan chat --provider local
lisan chat --provider openai
lisan agent writer --provider anthropic --dry-run "..."
```

Copy the template and edit `config.yaml` (gitignored, so endpoints and keys stay local) to change defaults and per-agent routing:

```bash
cp config.example.yaml config.yaml
```

Relevant environment variables: `LISAN_VAULT`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `CODEX_BIN`, `LISAN_BACKUP_RECIPIENT`.

Check provider readiness any time with `lisan provider check`.

## Command reference

Core lifecycle:

```bash
lisan init | validate | manifest | rebuild-index | sync | health | purge
```

Capture & conversation:

```bash
lisan capture --conversation-id demo "I had a weird day at work"
lisan conversation show | history | digest | reset --conversation-id demo
lisan chat
```

Records (manual creation, editing, archiving):

```bash
lisan new entity "Ada Lovelace"
lisan new episode "First Meeting"
lisan new decision "Use the CLI"
lisan new loop "Follow up with Sam"
lisan new knowledge "Vault architecture"
lisan new evidence "Screenshot 1" --artifact-text "..."
lisan new state work "Work is in setup mode."
lisan show <id> | edit <id> | archive loop <id>
lisan evidence correct ...      # append-only evidence corrections
lisan entity epoch ...          # roll an entity into a new epoch
```

Review, drafts & maintenance:

```bash
lisan review | review batch [--write]
lisan draft review --path "$LISAN_VAULT/drafts/<file>.md" [--apply]
lisan draft promote --path ...
lisan stale | loops | decay        # state freshness, open loops, confidence decay
lisan primer-audit
lisan dreamer <compress|primer|contradict|confidence|epoch|overfitting|identity-anchor>
lisan analyst scan | patterns audit
```

Ingestion (discover and process local artifacts):

```bash
lisan ingest scan <path> | status | run | show <id> | audit
lisan ingest batches | batch show|audit|quarantine <id>
```

Inspection & ops:

```bash
lisan assemble "Need context for the work domain"
lisan heuristic "forget this"
lisan complete "..."                 # raw prompt through the provider router
lisan prompts | prompt show writer_episode_v1
lisan agent <listener|assembler|writer|dreamer|advice|elicitor> [--task ...] [--dry-run] "..."
lisan logs [N] | traces recent | traces show <id>
lisan jobs run|list|show|retry|cancel|audit|reap-stuck   # durable background job queue
lisan backup status|create|test
lisan migrate
```

`purge` deletes the active vault, backups, and indices, then reseeds a fresh start; it prompts before doing so (use `--yes` plus `--preserve-config` / `--backup-before` for non-interactive runs).

## Backups

Local and deterministic. `lisan backup create` archives the vault plus the SQLite/embedding/config artifacts, staging to avoid concurrent-write corruption. `backup create --test-restore` restores into a temp directory and validates the copy. If `age` is configured and `LISAN_BACKUP_RECIPIENT` is set, backups are encrypted. Runs are logged to `backup.md` at the vault root.

## Repository layout

```
lisan/cli.py        top-level command router
lisan/config.py     config loading and defaults
lisan/paths.py      repo/vault path helpers
lisan/providers/    provider abstraction + adapters (incl. embeddings)
lisan/agents/       agent classes and deterministic fallbacks
lisan/schemas/      JSON schemas for agent outputs and record validation
lisan/tools/        deterministic workflows: retrieval, capture, validator,
                    backup, ingest, jobs, manifests, heuristic gate, ...
lisan/frontmatter.py  JSON-frontmatter parser/writer
prompts/            versioned agent prompts
docs/               deeper notes (ingestion, jobs, graph retrieval, evidence/claims, ...)
SPEC.md             the full architectural specification
CHANGELOG.md        date-based build history
```

## Status

Lisan is MVP-ready and usable as a daily local memory vault. The remaining work is mostly refinement — prompt calibration for long Elicitor sessions, optional review automation, and future provider/model changes — not core plumbing.

## Contributing

When making a change, the typical pattern is: put deterministic logic in `lisan/tools/`, add or update a schema if an agent's output shape changed, expose it in `lisan/cli.py`, run `lisan sync`, and verify the generated artifacts and logs. Keep new behavior deterministic where the deterministic-first hierarchy allows it.

Versioning is date-based: `YY.M.D.N`, where `N` is the build counter for that day (e.g. `26.6.16.2`).

## License

MIT — see [LICENSE](LICENSE). Designed for one life, adaptable to any.
```
