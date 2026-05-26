# Changelog

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
