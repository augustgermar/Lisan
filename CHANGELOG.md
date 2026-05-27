# Changelog

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

- Switched the default provider from `codex` to `local` in `config.yaml`, `lisan/config.py`, `lisan/providers/config.py`, and the README so a fresh checkout assumes a local model server rather than the Codex CLI.
- `startup_check` now runs a real reachability probe for the `local` provider via `diagnose_provider`, surfacing the connection error and any suggested fixes (instead of the generic "set CODEX_BIN" message) when the local server is unreachable.
- Added a regression test that the startup screen reports a clear local-provider error when the probe fails.

## 0.1.7

- Entity fanout deduplicates against existing canonical names and aliases — repeated short / full name variants (Devon vs. Devon Park) now fold into a single record with the variant appended to `aliases` instead of creating a sibling file.
- Codex provider retries once on truncated-JSON responses before raising; `lisan capture` catches `ProviderError` and prints a one-line message instead of a stack trace.
- Writer prompts (episode, decision, open_loop, state) now ask for per-entry `confidence_basis` on `state_updates`, `open_loops_to_create`, `decisions_to_create`, `claims_to_create`, `entities_to_create`, and `evidence_to_create`; `new_claim` accepts a `confidence_basis` argument so the writer's reasoning survives fanout.
- Open-loop ownership is enforced in both prompts and fanout: only loops owned by the user are materialized, so other people's pending questions stop becoming the user's todos.
- Evidence runs before claims in the fanout, and a new `resolve_evidence_links` helper rewrites writer-supplied evidence titles (e.g. "Transcript note: Devon staffing reflection") into resolvable `evidence.<slug>` IDs on `supporting_evidence` / `contradicting_evidence`.
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

- Renamed the public memory concept from arenas to domains.
- Introduced compatibility shims for legacy arena field names.
- Sanitized the checked-in seed vault and removed personal data from tracked primer files.
- Renamed state-facing terminology from arena to category in the runtime and prompts.

## 0.1.0

- Initial tracked release version for the Lisan project.
