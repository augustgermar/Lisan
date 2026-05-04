# Lisan

Local-first Python CLI framework for the Lisan memory vault.

This repository now includes the deterministic substrate plus the first memory pipeline:

- Deterministic vault validation
- Manifest generation
- SQLite index rebuild
- Heuristic gating and mode classification
- Provider abstraction for OpenAI, Anthropic, Google, and local models
- Default provider routing uses the local Codex CLI, so no API key is required to get started
- Listener -> Writer -> Skeptic -> Interlocutor capture pipeline
- Task-aware draft promotion into episodes, decisions, open loops, state, knowledge, or entities
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
- Optional local Codex binary override:
  - `CODEX_BIN`

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
python3 -m lisan prompts
python3 -m lisan prompt show writer_episode_v1
python3 -m lisan agent assembler "Need context for the work arena"
python3 -m lisan agent listener "forget this"
python3 -m lisan agent writer --task questions --dry-run "What should I ask next?"
python3 -m lisan transcript append --conversation-id demo --speaker USER "Hello world"
python3 -m lisan capture --conversation-id demo "I had a weird day at work"
python3 -m lisan primer-audit --dry-run
python3 -m lisan migrate --apply
python3 -m lisan review
python3 -m lisan draft review --path lisan-vault/drafts/your-draft-file.md
python3 -m lisan draft promote --path lisan-vault/drafts/your-draft-file.md
python3 -m lisan show --path lisan-vault/state/work-current.md
python3 -m lisan entity epoch --path lisan-vault/entities/people/ada-lovelace.md --summary "Ada entered a new phase."
python3 -m lisan new entity "Ada Lovelace"
python3 -m lisan new episode "First Meeting"
python3 -m lisan new decision "Use CLI"
python3 -m lisan new loop "Follow up"
python3 -m lisan new knowledge "Vault Architecture"
python3 -m lisan new evidence "Screenshot 1" --artifact-text "sample artifact"
python3 -m lisan new state work "Work is currently in setup mode."
python3 -m lisan state update work "Work is now active."
python3 -m lisan evidence correct --path lisan-vault/evidence/records/2026-05-03-screenshot-1.md --field timestamp --original "old" --corrected "new" --basis "manual correction"
python3 -m lisan edit --path lisan-vault/open_loops/2026-05-03-next-steps.md --set status=resolved --append-body "Resolved after implementing the edit command."
python3 -m lisan archive loop --path lisan-vault/open_loops/2026-05-03-next-steps.md
python3 -m lisan assemble --arena work Lisan
python3 -m lisan heuristic "I need to remember this"
python3 -m lisan complete "Summarize the current vault state"
python3 -m lisan sync
```

Writer and Dreamer tasks can select a specific prompt template, and `--dry-run` prints the composed prompt without calling a provider:

```bash
python3 -m lisan agent writer --task questions --dry-run "What should I ask next?"
python3 -m lisan agent dreamer --task confidence --dry-run "Review confidence decay candidates"
python3 -m lisan agent dreamer --task primer --dry-run "Build the yearly primer"
python3 -m lisan dreamer confidence
python3 -m lisan dreamer contradict
python3 -m lisan dreamer primer
```

`capture` always appends a transcript entry first. If the heuristic gate says the text matters, it then runs the Listener, Writer, Skeptic, and Interlocutor stages and writes a draft. `draft promote` now chooses the destination record type from the draft task when possible.

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

The next phase will expand the live provider-backed Writer/Skeptic/Interlocutor behavior and add richer editing flows on top of the manual record creators.
