# Evaluation loop — IG-11 round, findings log

Protocol: simulated August-persona conversations via `evals/driver.py` against
the live vault; every finding gets a code fix + regression test in the same
cycle. Axes: memory precision, thread continuity, deference, action+approval,
self-awareness, capability honesty, corrections, scheduling, strategy.

## Cycle 1 — 2026-07-03

| # | Scenario | Finding | Fix | Status |
|---|----------|---------|-----|--------|
| 1 | "how are the girls doing?" | Kinship shorthand ungrounded: answered about the cat, then about sister-in-law + niece. Conversation agent never saw who the user IS; identity-core roster was empty; dreamer never populated primer Key Relationships. | Owner profile (primer identity + roster) injected into every conversation turn; prod roster populated. Systemic dreamer-maintenance gap still open. | fixed, retested ✓ (grounds to Maya + Josie) |
| 2 | "email Josie my schedule" | Implied it could send email (not built). | Not-built list names SENDING explicitly; draft-vs-send distinction in prompt. | fixed, retested ✓ ("I can't send emails... can draft") |
| 3 | "what are you working on, anything stuck?" | — | — | pass ✓ (self_state; named stuck approval, open question, failed plan) |
| 4 | "correction: Josie is 12" | **Boundary violation**: executor edited the owner's OBSIDIAN note (restructured it with Lisan frontmatter + role tokens) instead of the Lisan entity; claimed "Done." | Executor sandboxed to workspace-write scoped to the Lisan install; default workspace = install root, never $HOME; HARD WRITE BOUNDARY paragraph in every executor briefing. Obsidian file restored (Summary line reconstructed — flagged to owner). Lisan-side update did not land in josie.md (only a draft record) — correction fan-out follow-up open. | fixed, retested ✓ (correction landed in entities/people/maya.md; Obsidian untouched) |
| 5 | "something feels off with your memory — figure it out and fix what you safely can" | — | — | pass ✓✓ (read self_state, found real failed job, created 5-step self-diagnostic plan autonomously) |

Open items for cycle 3:
- Dreamer should maintain primer Key Relationships + roster from conversation.
- "the girls" fix relies on injected profile; entity-linking for kinship terms in retrieval could reinforce.
- Latency profile: conversation turns 7-28s on rotato; codex executor calls 60-115s.

## Cycle 3 — 2026-07-03

| # | Scenario | Finding | Fix | Status |
|---|----------|---------|-----|--------|
| 6 | 3-turn thread: party planning → dishwasher interruption ("remind me tomorrow 9am") → back to party | — | — | pass ✓ (reminder actually scheduled and verified; unprompted return to the party thread; honest "no records of past party sizes") |
| 7 | "what do my notes say about [the sensitive family project]?" | Accurate, source-cited answer — but a ~99s run_codex detour to read records it already holds. | Tool-preference guidance in the conversation prompt: search_memory/read_file for reading own records; run_codex only for acting. Retrieval ranking of knowledge records vs recent conversation noted for a future cycle. | fixed (guidance); latency watch open |
| 8 | Voice: self-deprecating double-booking confession | — | — | pass ✓ (deadpan register present, then practical help; no internal mechanics leaked) |
| 9 | "add a note that the repair guy is named Hector, did good work" | — | — | pass ✓✓ (entity created in the Lisan vault; personal notes untouched — boundary held in natural flow; claim verified true) |

Open items for cycle 4:
- Dreamer should maintain primer Key Relationships + roster from conversation (systemic; roster was hand-populated this round).
- Retrieval ranking: knowledge records vs recent conversational records for "what do my notes say about X" questions.
- Latency: conversational turns 9-28s; knowledge-chase turns up to 99s pre-guidance.

## Cycle 4 — 2026-07-03

| # | Scenario | Finding | Fix | Status |
|---|----------|---------|-----|--------|
| 10 | "when is the younger daughter's appointment?" (event captured the previous day as "today") | **Temporal staleness, three layers deep**: writers froze relative words at write time; the frozen phrase replicated into entity summaries (including two *place* entities), the state record, and the entity story; retrieval rendered no record dates so the reader couldn't resolve them. | TIME RULE in every writer prompt (all 11 variants) + writers receive TODAY; retrieval renders record_date on every item; conversation prompt treats stored relative words as frozen-at-write-time; live records swept and absolutized. | fixed, retested ✓ ("appointment is on July 2, 2026") |

Open items for cycle 5:
- Past-tense awareness: a dated past event should be phrased "was", not "is".
- Phrase replication across records (one fact echoed into place entities + state) — capture dedup/normalization worth a look.
- Dreamer primer maintenance (carried).

## Cycle 5 — 2026-07-03

| # | Scenario | Finding | Fix | Status |
|---|----------|---------|-----|--------|
| 11 | Same probe as #10 | The agent didn't know today's date, so a past event read "is on July 2" instead of "was". | TODAY (local date+time) injected into every conversation turn, with a tense-anchoring rule. | fixed, retested ✓ ("was yesterday, July 2nd") |

## Cycle 6 — 2026-07-03 (Hermes external examiner + my direct review)

Hermes ran 10 scenarios (30 turns) as an independent examiner; I reviewed the
transcripts and vault artifacts directly and used its report as a second
opinion. Both examiners independently found the same two defects; my artifact
review was stricter on the fast-path and traced the correction bug to the
record level.

| # | Scenario | Finding | Fix | Status |
|---|----------|---------|-----|--------|
| 12 | Fresh-turn "what can you do?", "what's up?", "help me get organized" | Canned fast-path (help/status/smalltalk) fired 0.0s boilerplate, bypassing the capability model — obsolete now that the agent answers in ~7s. | Fast-path shrunk to bare acknowledgments + identity (kept for latency + identity-bleed safety); everything else routes to the agent. | fixed, verified (classification) ✓ |
| 13 | Correction: state favorite band, correct it, re-ask | **Trust-critical**: correction half-persisted (state updated, old claim + entity summaries left active), and at recall the agent picked a stale value and FABRICATED "you confirmed X was still your favorite" — a history that never happened. Root causes: async capture lag (the correction hadn't been written yet), entity summaries each asserting "their favorite" as durable standalone facts, and confabulation under contradiction. | Conversation-precedence rule: for facts stated in THIS conversation, the verbatim history outranks lagging memory. Contradiction-resolution order (conversation > state.* > record_date) + a hard no-invented-history rule. | fixed, retested ✓ ("Based on what you just told me, your favorite band is David Bowie") |
| 14 | Provider-failure message (Hermes rec 3) | "I hit a provider failure" was opaque. | Now names the cause ("my language model didn't respond — transient, not your message") so the user knows to retry. | fixed ✓ |

Hermes scenarios that PASSED (my review concurs): recall-with-conflict-detection
(caught 15/12 vs stored 7/11 and asked), thread continuity through an
interruption + provider failure, temporal ("next Tuesday" → "July 7th", "4
days"), file read + neighborly path guessing, self-awareness/not-built honesty,
emotional register without mood leakage, voice ("break a leg").

Open items for cycle 7:
- Entity summaries assert time-varying facts ("their favorite") as durable —
  writer should scope such assertions or defer them to state. (deeper fix)
- Correction should retire/supersede the old claim, not just add a new state.
- Memory-update writes still spawn a full ~60-170s codex executor session;
  a direct record edit would be far cheaper.
- Dreamer primer maintenance (carried).

## Cycle 7 — 2026-07-03/04 (narrative-structure pressure test)

Owner asked to observe how entity narrative structure evolves as an entity
accumulates data — does it naturally adopt a three-act shape? Building the
experiment (evals/grow_entity.py: grow one entity across many turns, snapshot
the narrative shape at checkpoints) uncovered two regressions and answered the
question.

| # | Finding | Fix | Status |
|---|---------|-----|--------|
| 15 | **Living entity stories were silently DEAD.** The entity.rewrite_story jobs — the mechanism that grows/re-tells an entity's narrative, "the heart of the memory system" — were enqueued only inside capture_text (legacy path). The capture.observe observer bypassed it, so since the conversation-agent restructure NO entity story grew past its first stub. Hidden because 30 rewrite jobs showed "succeeded" — all stale, targeting pre-regression entities. | The observe dispatch enqueues a rewrite for every touched entity. | fixed, verified ✓ (Silas: 8-word stub → full arc-preserving biography) |
| 16 | Growth still stalled after ~2 turns: an entity was only "touched" when the writer NAMED it, but conversations shift to pronouns ("she never married") within a turn or two, and the isolated-turn background writer stops naming the subject. | Rewrite also fires for existing entities named anywhere in the recent conversation thread (bounded, coalesced). | fixed, verified ✓ (Orin: continuous 10→59→93→120 words through pronoun turns) |
| 17 | **The research answer**: structure does NOT emerge with complexity. The writer crammed a dozen life events into one dense 120-word paragraph and dropped the arc's resolution (recovery, the partner, reconciliation, the closing image all lost). Cause: the prompt asked for both "single flowing passage" AND "2-5 paragraphs" with a fixed length target, so accumulation was resolved by compression + loss. | Rewrite prompt now: length scales with the life; structure may emerge (origins → turning point → present, no headings); the arc's end is preserved. | fix deployed; A/B re-run in progress |

Open for cycle 8:
- Verify the revised prompt produces graduated structure + preserved resolution (A/B running).
- Entity-story growth adds latency to capture (background, so not user-facing) — watch coalescing keeps rewrite volume sane on busy conversations.
- Dreamer primer maintenance (carried).

## Cycle 8 — 2026-07-04 (clean narrative structure A/B + guardrail)

Clean 16-turn growth run (community garden, novel entity, no vault collision)
testing the prompt revision + no-shrink guardrail together.

RESULT: split verdict.
- ✓ STRUCTURE FIXED. A complex arc now becomes a proper 3-paragraph narrative
  following the shape (origins → struggle/resilience → present meaning),
  versus the old single dense paragraph. The prompt revision works.
- ✗ NEW BUG — entity fragmentation. The same garden became THREE entities:
  two `place` records with name variants ("Wisteria Hollows" vs "Wisteria
  Hollows community garden") + one bogus `person` record. The story split
  across them, so no single narrative is complete. Root cause: unstable kind
  (place on most turns, person on one) → shared tokens marked "ambiguous" in
  the dedup index → token-merging disabled → every later name variant spawns
  a new duplicate.
- ~ Content granularity: even the fuller records summarize away specific human
  detail (a beloved gardener reduced from "taught kids to graft roses, ashes
  under the apple tree per his wish" to "a person named Bertram whose ashes
  are in the soil"). The single-pass full-rewrite can't hold unbounded detail.

| # | Finding | Fix | Status |
|---|---------|-----|--------|
| 18 | Entity kind unstable across mentions → duplicate records of different kinds → ambiguous-token cascade → name-variant fragmentation. | Kind stickiness: once an entity exists under a name, later mentions inherit its kind rather than re-deciding. | fixed, tested ✓ (place never duplicated as person) |

Open for cycle 9:
- Name-variant merge within a kind ("X" vs "X community garden") — kind
  stickiness removes the ambiguous-token poison that blocked it; verify it now
  merges in a clean run.
- Content granularity in long rewrites — the single-pass summarize limit.
  Candidate: stable-core + recent-tail accretion for very rich entities.
- Dreamer primer maintenance (carried).
