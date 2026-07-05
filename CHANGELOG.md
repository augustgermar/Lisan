# Changelog

## 26.7.4

Skills platform: installable conversation tools, ported from the Hermes
agent's skill catalog.

- **Bundled skills** under `skills/`, opt-in via `lisan skills
  list/install/uninstall`: Gmail (search/read/send with a stdlib OAuth
  onboarding broker — credentials are user-provisioned, never bundled),
  iMessage (recent/history/search/send via the `imsg` CLI), Obsidian
  (search/read, strictly read-only with traversal guards), OpenStreetMap
  maps, arXiv search, YouTube transcripts, and Polymarket. All standard
  library; external services degrade to readable errors.
- **Per-skill approval gating.** `"requires_approval": true` in a skill's
  schema.json routes every call through the same approval channel as codex
  (interactive prompt in CLI chat, approve/deny buttons on Telegram); with
  no channel available the action is refused. `gmail_send` and
  `imessage_send` ship gated.
- **`lisan skills setup <name> -- <args>`** forwards to a skill's credential
  setup script (`--check` / `--client-secret` / `--auth-url` / `--auth-code`
  / `--revoke` for Google), so the agent can drive onboarding over any
  conversation surface.
- **Fix:** `python -m lisan` now propagates the CLI's exit code
  (`__main__.py` dropped it), which the setup broker's `--check` contract
  relies on.

Phase 2: functional self-awareness architecture (docs/phase2_roadmap.md).

- **The identity kernel is enforced.** `primer/identity-core.md` is
  write-gated (ceremony-only), content-hashed with recorded drift events,
  and its ratified `## Voice` section supersedes the authored prompt voice —
  identity is carried by the vault, so an engine swap carries the voice.
- **The voice ceremony.** `lisan self extract-voice` distills voice
  invariants from the agent's own transcript history behind a hard evidence
  gate (3+ verbatim quotes across 2+ conversations, factory/earned
  provenance); `lisan self ratify` writes them into the kernel. Run live:
  349 turns across 56 conversations produced 6 evidence-gated invariants.
- **Layer B: first-person memory.** `self_episode` records assembled
  deterministically from job/plan/ceremony records (confabulating one's own
  history is structurally impossible); `self_belief` records with chained,
  evidence-required revisions, reconciled against the episodic record by
  the new `lisan dreamer reconcile` task.
- **Drive v1.** Deficit-scored open loops (salience + stake + age, decaying
  to zero) can open a fresh session with one question-phrased callback,
  cooldown-stamped; graduated autonomy via `drive.action_tier`, enforced in
  code, shipped at tier 0.
- **Eval instrumentation.** Kernel-derived consistency rubric, a
  different-family model judge, behavioral metrics, a fixed 13-probe
  baseline (captured), and the Wipe Test — which the layer separation
  passed: voice fingerprint unchanged on a memory-wiped clone while the
  autobiography vanished.
- **Hardening.** Executor workspace can no longer widen past home on a
  disjoint vault; five silent-degrade exception sites in the capture gate,
  primer index, owner profile, and validator now log or report.

## 26.7.3

- **Conversation became an agent; memory capture became an observer.** Every
  non-trivial turn now goes to one tool-bearing agent with the rolling
  conversation verbatim, retrieved memory, an owner profile, capabilities, and
  the current date/time; the listener/writer/skeptic pipeline runs afterward as
  a background `capture.observe` job. Turn latency dropped from 60–235s to
  ~7–25s.
- **Reasoning agents run on hosted Gemini via the rotato proxy**; codex remains
  the delegated executor, sandboxed to the install with a hard write boundary
  (it never touches files outside Lisan).
- **New capabilities**: generated self-model (`lisan self`, `self_state` tool,
  auto-regenerated `primer/capabilities.md`); durable multi-step plans on the
  job queue (`lisan plan`, `create_plan`, `plan ingest-folder`); real-time
  scheduler and tasks (`lisan task`, `lisan scheduler`); Telegram approval
  buttons and an interactive approval flow.
- **Temporal cognition**: writers absolutize relative dates, retrieval renders
  record dates, and the agent knows what day it is and answers in correct tense.
- **CLI restyle**: codex-style blue/grey palette with a LISAN wordmark; the
  redundant "You:" prompt is now a "›" caret.
- **Fixes surfaced by the automated evaluation loop**: kinship-shorthand
  grounding, capability honesty (no phantom email sending), action requests
  never skipped, recall questions answered instead of captured, silence never
  returned as a reply, config.json rename, and a repo-wide personal-data scrub.
- Bumped version to 26.7.3.

## 26.6.19.1

- Hardened the fresh-repo plumbing and evaluation readiness: `lisan health` and `lisan sync` now bootstrap the vault layout, seed files, and SQLite schema instead of failing on a blank checkout; skip/retrieval turns now always return a user-facing answer; successful fanout no longer leaves drafts stuck in the queued backlog; transient provider failures retry with backoff; and the job-drain, bootstrap, retry, skip-response, and embedding behaviors are covered by new regression tests. The shipped defaults and example config now point at the local Gemini/Rotato endpoint on `127.0.0.1:8990`, with local routing as the tracked default.
- Bumped version to 26.6.19.1.

## 26.6.16.2

- Entity kind model (P3): `kind` is now a first-class, open property of every entity (person, pet, agent, organization, place, system, artifact, project, event, topic, account, or `thing`), replacing the people-shaped default that forced non-people — a project ("Atlas"), a city ("Houston") — to become *people*. Kind is assigned deterministic-first: roster (a structured cast in `identity-core.md`: name + aliases + kind) → structural signals (IP/host/path/URL/org-suffix → system/artifact/organization) → the model's explicit choice → `thing` as the honest fallback. `person` is never a default at any layer. Dedup is kind-scoped (a person "Atlas" and a project "Atlas" never merge), and the kind set is open — novel kinds are stored, not rejected. Fixes the I2 entity false-positive finding. (Migration of existing pre-kind records and the typed-relationship graph are deferred to follow-up specs.)
- Bumped version to 26.6.16.2.

## 26.6.16.1

- Auto-index records on capture (C2/C3): fanned-out records (claims, decisions, open loops, state, entities, relationships) are now indexed into the SQLite `files` table and FTS the moment they are written, so within-session and cross-conversation retrieval sees what an earlier turn just wrote — no manual `sync` required. Extracted `index_single_record()` as the single source of truth for per-file indexing (used by both the full rebuild and the incremental path), with INSERT OR REPLACE / delete-then-insert for idempotent upserts. Embedding stays deferred (`embedding_status='pending'`) to the async sweep so capture never blocks on the embedder; one SQLite connection is reused per turn.
- Bumped version to 26.6.16.1.

## 26.6.15.1

- Hardened the deixis layer against weak writer models: the interlocutor payload now deterministically tokenizes the principal's name aliases to `{{principal}}` before rendering, so a writer that emits the principal's literal name instead of the token no longer leaks it into the spoken reply. Added `tokenize_principal()` plus unit and regression tests.
- Fixed claim evidence links: unresolvable natural-language evidence titles are now dropped instead of being stored as dangling link targets that fail vault validation.
- Bumped version to 26.6.15.1.

## 26.6.13.4

- Rendered deixis at the conversational and display boundaries so the interlocutor sees second person and human-facing reports show the principal name, while the substrate keeps role tokens internally. Tightened the pipeline regression test around the summary boundary.
- Bumped version to 26.6.13.4.

## 26.6.13.3

- Added `lisan telegram install-service` / `uninstall-service`: install the Telegram bot as an always-on OS service (launchd on macOS, systemd `--user` on Linux) so it auto-starts on login and restarts if it crashes — no terminal left open. The generated unit runs `lisan telegram run` against the configured vault; the token stays in the gitignored `config.yaml`, never in the service file. Unit/plist rendering is unit-tested.
- Bumped version to 26.6.13.3.

## 26.6.13.2

- Added a Telegram bridge (`lisan telegram run`): talk to Lisan from Telegram using the same capture pipeline as the CLI, so messages are remembered and recalled identically. Long-polling, stdlib-only (`urllib`, no new dependencies), per-chat conversation state, an allowlist (`LISAN_TELEGRAM_ALLOWED`) so only your own user id is answered, a "typing" indicator during generation, reply chunking to Telegram's 4096-char limit, and `/new` / `/domain` / `/logs` / `/help` commands. Token via `LISAN_TELEGRAM_TOKEN` (or a gitignored `telegram:` block in `config.yaml`).
- Added a setup wizard (`lisan telegram setup`): validates the bot token live via `getMe`, then auto-detects your numeric user id by watching for a message you send the bot (no @userinfobot lookup needed), and saves the token + allowlist into the gitignored `config.yaml`.
- Bumped version to 26.6.13.2.

## 26.6.13.1

- Stopped tracking the live `config.yaml` (now gitignored) and added a tracked `config.example.yaml` template. `config.yaml` holds machine-specific routing/endpoints (e.g. a local Ollama embedding server) that should not live in the public repo; the app already falls back to the built-in `DEFAULT_CONFIG` (identical to the shipped default) when it is absent, so this is behavior-neutral for clones. Copy `config.example.yaml` to `config.yaml` to customize locally.
- Hardened `.githooks/pre-commit` with a guard that refuses to commit personal/local files — anything under `lisan-vault/`, plus `config.yaml`, `lisan.sqlite`, and `embeddings.bin` — so vault data and live config can't be published by accident.
- Bumped version to 26.6.13.1.

## 26.6.12.2

- `install.sh` now builds the SQLite index (`rebuild-index`) right after seeding the vault, so index-backed commands (`health`, retrieval, chat context) work on a fresh install instead of failing with "no such table: files".
- Bumped version to 26.6.12.2.

## 26.6.12.1

- Added `install.sh`, a one-line installer (`curl -fsSL https://raw.githubusercontent.com/augustgermar/Lisan/main/install.sh | bash`). It finds a Python >= 3.11 interpreter and git, clones into `~/.lisan/repo`, builds an isolated virtualenv at `~/.lisan/venv`, does an editable install (so a later `git pull` updates the CLI in place), writes a `lisan` launcher to `~/.local/bin` that defaults `LISAN_VAULT` to `~/.lisan/vault` while honoring an externally-set value, seeds the vault, and wires up PATH. Fully non-interactive (pipe-safe) and re-runnable; tunable via `LISAN_HOME`, `LISAN_BIN_DIR`, `LISAN_VAULT`, `LISAN_REF`, `LISAN_EMBEDDINGS`, `LISAN_NO_INIT`, and `LISAN_NO_PATH`.
- Fixed `pip install` from a clean clone: `pyproject.toml` listed `lisan.evals` in `[tool.setuptools] packages`, but that directory carries no tracked source, so setuptools failed every build with "package directory 'lisan/evals' does not exist". Removed it from the package list (nothing imports it).
- Bumped version to 26.6.12.1.

## 26.6.7.2

- Added FastEmbed (Qdrant's ONNX, CPU-only, no PyTorch) as an in-process embedding backend behind the existing provider abstraction, selectable via `retrieval.embeddings.provider = "fastembed"` (now the default). The `TextEmbedding` model is lazily imported and instantiated exactly once per process (singleton keyed by model name + cache_dir), reused by every query and record.
- Shipped as an optional extra: `pip install lisan[embeddings]`. Installing the extra IS the activation — with the default `provider: fastembed` + `mode: auto`, semantic retrieval turns on the moment the package is importable, with no config flag. A base `pip install lisan` runs keyword-only: a missing `fastembed` package is treated as an unreachable embedder (honors `unreachable_policy`, default `skip`), warns once per process with an install hint, and caches the unavailable state so the import is not retried. `requirements.txt` stays stdlib-only.
- Applied the BGE query/passage distinction correctly (the silent-quality footgun): FastEmbed's native `query_embed`/`passage_embed` are a no-op for the default `BAAI/bge-small-en-v1.5` model, so Lisan defaults `query_prefix`/`passage_prefix` to the model's documented convention (queries get the instruction prefix, passages none). Records embed with the passage form, queries with the query form. Setting both prefixes to `null` defers to FastEmbed's native methods; custom strings support other models. Changing the model or the `passage_prefix` requires a full `rebuild-index`; changing only the `query_prefix` does not (query vectors are computed fresh per query against the existing bare passage vectors).
- Observed dimension stays authoritative (BGE default = 384 written to the `embeddings.bin` header, not the config hint); FastEmbed's generator of numpy arrays is materialized and converted to `list[float]` for the existing JSON-lines store, using native batching.
- New `cache_dir` config key for FastEmbed weights, honoring `$FASTEMBED_CACHE_PATH`, defaulting to `~/.cache/lisan/fastembed` (never the system temp dir). Documented the one-time ~90MB download.
- Default config provider changed from `local` to `fastembed`; default model `BAAI/bge-small-en-v1.5`; default dimension hint 384. The OpenAI-compatible HTTP endpoint remains available via `provider: local`.
- Bumped version to 26.6.7.2.

## 26.6.7.1

- Replaced the SHA256 hash "embedding" placeholder with real local-first semantic embeddings. New `EmbeddingProvider` (`lisan/providers/embeddings.py`) talks to an OpenAI-compatible `POST {base_url}/v1/embeddings` endpoint (llama.cpp / LM Studio / Ollama-compatible, or hosted OpenAI/Google via an OAI-compatible endpoint), with an optional, lazily-imported `sentence-transformers` backend. The deterministic `hash_embedding` is kept but demoted to an explicit fallback.
- New `retrieval.embeddings` config block with a tri-state `mode` (`auto` | `semantic` | `hash`) and an `unreachable_policy` (`skip` | `hash`). `auto` (default) uses semantic embeddings whenever a server answers and fails over per policy when it does not; the embed attempt itself is the reachability probe and connection-refused fast-fails without waiting out `timeout_seconds`. `hash` never touches the network (reproducible CI baseline).
- Fixed the retrieval performance trap: the query is now embedded exactly once per retrieval call and `embeddings.bin` is loaded once into an mtime-cached in-memory map, instead of re-embedding the query and rescanning the whole file for every candidate.
- Fixed the dimension-mismatch trap: `embeddings.bin` now carries a model+dimension header, the authoritative dimension is whatever the embedder actually returns (config `dimensions` is a hint only), and cosine scoring skips (never truncates) vectors whose dimension differs from the live query model — with a loud warning telling the operator to run `rebuild-index`. Switching the embedding model requires a full `rebuild-index`.
- `unreachable_policy: skip` (default) writes no vectors for records embedded while the server was down and flags them `embedding_status='pending'` (new `files` column). Pending records are re-embedded on the next full `rebuild-index`, or incrementally via the new `index.embed_pending` job — no hash vectors are ever written into a semantic index.
- `retrieval_log` now records the actual embedding mode used per call (`semantic` | `hash` | `skip`) via the new `embedding_mode` column, and `lisan health` shows the active mode, embedder reachability, the index model + dimension, and the count of pending records.
- Bumped version to 26.6.7.1.

## 0.1.11

- Removed local testing leftovers from the workspace, including the generated SQLite index and vault transcripts/logs, so a fresh checkout is back to a plain open-source-friendly codebase.
- Reworded README, docs, prompts, and diagnostics so user-facing references use generic "coding agent" terminology instead of explicit Codex branding where possible.
- Bumped the package version for the cleaned release.

## 0.1.10

- Added hybrid retrieval: SQL, FTS, and vector layers each return their own ranked candidate sets, which are then combined with Reciprocal Rank Fusion (RRF) instead of summed into a single additive score. Layer signals stay separable, so a vector hit and a domain hit no longer cancel out as interchangeable numbers in the same bucket.
- New `retrieval.fusion` config block (`enabled`, `method`, `rrf_k`, `per_layer_limit`, `fused_limit`) in both `config.yaml` and the default config.
- Extended the `retrieval_log` SQLite table with per-layer telemetry: `retrieval_mode`, `fusion_enabled`, `sql_candidate_count`, `fts_candidate_count`, `vector_candidate_count`, `fused_candidate_count`, `overlap_count`, `rrf_k`, `per_layer_limit`, `fused_limit`, `fts_mode`.
- Added regression tests covering the new fusion behaviour.

## 0.1.9

- Split the writer's episode pass into two sequential calls: `writer_episode_core_v1` produces the body, summary, frontmatter, and `claims_to_create`; `writer_episode_artifacts_v1` produces the derived `entities_to_create`, `open_loops_to_create`, `decisions_to_create`, `state_updates`, and `evidence_to_create`. The Skeptic and Interlocutor only see the core; the artifact call only runs when Skeptic approves the core, so a rejected draft never spends a second writer call.
- Non-episode writer tasks (decision, open_loop, state, knowledge, entity, questions) stay single-shot — they're already small.
- Added `_merge_writer_outputs` so the downstream fanout still sees a single merged dict and required no changes.
- Added a regression test that asserts the episode path makes exactly two writer calls, the first using the core prompt and the second using the artifact prompt with `PRIOR_WRITER_CORE` in its input.

## 0.1.8

- Switched the default provider from `codex` to `local` in `config.yaml`, `lisan/config.py`, `lisan/providers/config.py`, and the README so a fresh checkout assumes a local model server rather than the coding agent CLI.
- `startup_check` now runs a real reachability probe for the `local` provider via `diagnose_provider`, surfacing the connection error and any suggested fixes (instead of the generic "set CODEX_BIN" message) when the local server is unreachable.
- Added a regression test that the startup screen reports a clear local-provider error when the probe fails.

## 0.1.7

- Entity fanout deduplicates against existing canonical names and aliases — repeated short / full name variants now fold into a single record with the variant appended to `aliases` instead of creating a sibling file.
- The coding agent provider retries once on truncated-JSON responses before raising; `lisan capture` catches `ProviderError` and prints a one-line message instead of a stack trace.
- Writer prompts (episode, decision, open_loop, state) now ask for per-entry `confidence_basis` on `state_updates`, `open_loops_to_create`, `decisions_to_create`, `claims_to_create`, `entities_to_create`, and `evidence_to_create`; `new_claim` accepts a `confidence_basis` argument so the writer's reasoning survives fanout.
- Open-loop ownership is enforced in both prompts and fanout: only loops owned by the user are materialized, so other people's pending questions stop becoming the user's todos.
- Evidence runs before claims in the fanout, and a new `resolve_evidence_links` helper rewrites writer-supplied evidence titles into resolvable `evidence.<slug>` IDs on `supporting_evidence` / `contradicting_evidence`.
- Heuristic gate's affect lexicon and `_has_distress_signal` cover the distress / fear vocabulary that the prior list missed (`scared`, `fear`, `worried`, `panic`, `blindsided`, etc.).
- Conversation turn position is computed from the transcript instead of narrative state, so the Turn-1 elicitor preference fires only on actual opening turns of extraction-only conversations.
- The cross-conversation "Recent Activity" preamble moved from the elicitor session into the assembler and is gated on a deterministic "fresh conversation" check, so the extraction path now also opens with awareness of today's other conversations.

## 0.1.6

- Gated state, evidence, and claim fanout on Skeptic approval; rejected drafts are held with `status: needs_revision` for review (decisions, open loops, and entity stubs stay exempt).
- Decoupled the Interlocutor from Skeptic flags so review-layer uncertainty no longer bleeds into the user-facing response.
- Wired writer-generated claims through to the SQLite `claims` table during fanout, and made `rebuild-index` index standalone claim records.
- Threaded `linked_claims` / `linked_episodes` and per-record `confidence_basis` through every fanout writer.
- Tightened deterministic domain assignment so records that name primer relationships land in `relational` / `work` instead of `cross_arena`.
- Added single-entity sentence-leading pronoun resolution before persisting state summaries.
- Preferred Elicitor on opening emotional turns so distress is heard before it is processed.
- Added a "Recent Activity (today)" preamble to first-turn Elicitor sessions for cross-conversation awareness.
- Added transcript deduplication to prevent duplicate user turns from crashed / timed-out captures.
- Made `lisan capture` quiet by default (only Lisan's spoken response); added `--verbose` for the full pipeline JSON.
- Rewrote the Interlocutor prompt to acknowledge resolution moments and to drop references to the review layer.

## 0.1.2

- Hardened Analyst pattern generation against overfitting and duplicate hypotheses.
- Added formal pattern lifecycle governance and Dreamer eligibility checks.
- Added pattern audit tooling and anti-diagnosis validation.
- Removed publish-time personal identifiers from package metadata and example text.

## 0.1.1

- Renamed the public memory concept from arenas to life areas.
- Introduced compatibility shims for legacy arena field names.
- Sanitized the checked-in seed vault and removed personal data from tracked primer files.
- Renamed state-facing terminology from arena to category in the runtime and prompts.

## 0.1.0

- Initial tracked release version for the Lisan project.
