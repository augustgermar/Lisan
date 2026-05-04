# Lisan

Local-first Python CLI framework for the Lisan memory vault.

This repository currently contains the Phase 1 skeleton:

- Deterministic vault validation
- Manifest generation
- SQLite index rebuild
- Heuristic gating and mode classification
- Provider abstraction for OpenAI, Anthropic, Google, and local models
- A seed vault with example records and git hooks

## Requirements

- Python 3.11+
- Optional API keys if you want live provider calls:
  - `OPENAI_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `GOOGLE_API_KEY`
- Optional local model endpoint:
  - `LISAN_LOCAL_MODEL_URL`
  - `LISAN_LOCAL_MODEL`

## Quick start

Initialize the workspace:

```bash
python3 -m lisan init
```

Run the core deterministic checks:

```bash
python3 -m lisan validate
python3 -m lisan manifest
python3 -m lisan rebuild-index
python3 -m lisan health
```

Useful commands:

```bash
python3 -m lisan assemble --arena work Lisan
python3 -m lisan heuristic "I need to remember this"
python3 -m lisan complete "Summarize the current vault state"
python3 -m lisan sync
```

## Vault

The vault lives in `lisan-vault/` and uses markdown files with JSON frontmatter for deterministic parsing.

Generated artifacts:

- `lisan-vault/manifests/`
- `lisan.sqlite`
- `embeddings.bin`

Git hooks are configured locally via `.githooks/`:

- `pre-commit` runs manifest generation and validation
- `post-commit` rebuilds the index

## Scope

Phase 1 is the boring substrate. The next phase will add CRUD commands for entities, episodes, decisions, and open loops, followed by the agent pipeline.
