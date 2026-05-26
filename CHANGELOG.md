# Changelog

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
