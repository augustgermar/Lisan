# Architecture review: vellum-assistant vs. Lisan — 2026-07-04

Subject: https://github.com/vellum-ai/vellum-assistant (MIT, TypeScript/Bun,
~8k files; reviewed from a local read-only clone at depth 1). Vellum AI is a
for-profit company shipping this as open source plus a managed platform.

## What it is

The same thesis as Lisan, commercialized: "Personal Intelligence" — a
distinct, named assistant per person ("hatch your assistant"), shaped by
its relationship with one "guardian," with memory as the moat. Their
CONSTITUTION.md is a genuinely good document: relationship archetypes
(Contractor → Coworker → Confidant → Friend), "recovery is where trust is
built," four named trust-eroding failure modes (silent failure,
misrepresentation, irreversible action without informed consent,
manipulation). Their counter-positioning explicitly dismisses our
architecture class ("Not a SQLite + markdown file you maintain yourself")
— and then their newest memory model (v3) stores knowledge as… markdown
pages with sections, organized in domain/topic leaves. Convergent
evolution is the theme of this review.

Stack: Bun daemon + Postgres/Drizzle + Docker sandboxing + desktop/iOS/web
clients + eight channels + OAuth to nine services. The weight (8k files)
buys product surface, not memory science.

## Convergent with Lisan (independent confirmation of our choices)

- Narrative as a first-class memory type, plus a monthly "narrative arc
  refinement" job.
- Identity injected from files (SOUL.md / IDENTITY.md), not hardcoded in
  prompts — voice from data.
- Proactivity framed around unfinished threads and follow-ups; a
  first-person journal; a retrospective capture sweep.
- Memory as markdown pages with heading-delimited sections (v3), hybrid
  sparse+dense retrieval, per-record staleness, deterministic decay ticks.
- Default-deny permissions, per-tool sandboxing, approval grants
  (once / 10 minutes / always), audit-everything ("Vellum Doctor").

## Worth borrowing (ranked, with fit)

1. **Reply-query retrieval pass.** They retrieve not only on the user's
   message but on the assistant's own *previous reply* as separate
   queries — surfacing threads the agent is developing that the user
   references without naming ("do it", "the second one"). Cheap, fits
   `retrieval.py` as one extra query leg. Directly attacks a continuity
   failure class we've seen.
2. **Learned edges from co-selection.** They mine which memories get
   *selected together* (NPMI graph) and use it as a retrieval expansion
   lane — retrieval that learns from its own usage, deterministically. We
   already log every retrieval in `retrieval_log`; this is a
   deterministic-first, Lisan-native upgrade waiting in data we already
   collect.
3. **Embedding anisotropy correction** (Mu & Viswanath "all-but-the-top"):
   subtract corpus mean + top principal component(s), renormalize, persist
   the calibration per (provider, model, dim). Fixes compressed cosine
   ranges (they call Gemini embeddings the worst offender). Applies
   directly to our BGE/FastEmbed leg; pure deterministic post-processing.
4. **Serendipity slots.** Reserve 1–2 retrieval slots for weighted-random
   picks from the 30th–70th percentile of scored candidates — prevents the
   same records always loading, creates unexpected associations. Trivial
   to add at our RRF fusion stage; also a hedge against retrieval ruts,
   which our overfitting dreamer task worries about from the other end.
5. **Retrospective capture sweep.** A periodic job re-reads conversation
   deltas since its last successful run and captures anything missed
   in-the-moment, with a dedup log of what was already remembered. Their
   correctness invariant is worth copying verbatim: the progress pointer
   advances ONLY on success; the cooldown pointer advances on every
   attempt. Fits as a dreamer task over our transcripts vs. captured
   records — a recall-safety net under the observer pipeline.
6. **Behavioral-contract fast lane.** Their SOUL.md norm: when the user
   states a working preference ("keep summaries to three lines", "don't
   ask, just do it"), write it down *the same turn* — "the wrong behavior
   will repeat until you write the right one down." Their implementation
   (agent free-edits its own identity file) is kernel-unsafe by our
   standards, but the *speed norm* is right and we lack it: explicit
   contracts should land in `operating-style.md` (accretive layer, never
   the kernel) immediately, provenance-tracked.
7. **Retrieval token budget from headroom.** Injection budget computed
   from live context headroom, clamped to [min, max] — instead of fixed
   limits. Small, sane.
8. **KV-cache-aware context assembly.** Their candidate pool renders a
   byte-identical stable prefix across turns specifically to ride provider
   prompt caching. At our 16–23s turn latency on rotato, ordering our
   assembled context (primer → capabilities → stable memory → volatile
   tail) for cache stability is free latency/cost.
9. **Trust rules as data + credential isolation** (product-phase, not
   now): declarative rules (tool / glob pattern / scope / allow-deny-ask /
   priority, deny wins ties) instead of code-enforced tiers only; and
   credentials held by a separate broker *process* so tokens never enter
   the model's reachable filesystem — our Telegram token sits in
   config.json today. Becomes load-bearing the moment the allowlist grows
   past the owner (kids, peer instances — Gap 3).
10. **Narrative hindsight elevation + typed emotional decay.** Their
    monthly job re-evaluates old details with hindsight — something minor
    becomes a "turning point" once later events reveal it. Beautiful fit
    for our entity stories as a dreamer task. Their decay curves are
    typed (linear / logarithmic / transformative / permanent — the last:
    intensity drops but never reaches zero); our single confidence-decay
    model is coarser.

## Where Lisan is ahead (and should stay)

- **The identity layer is unprincipled there.** SOUL.md is self-edited by
  the model, same-turn, no provenance, no gate, no hash — identity and
  memory are entangled. It would fail our Wipe Test as a *layer
  separation* claim; there is no equivalent of the ceremony, the evidence
  gate, factory/earned provenance, or wipe-proof kernel. This is our
  defensible research contribution.
- **Deterministic self-episodes.** Their journal is freeform model prose;
  our autobiography is assembled from records and structurally cannot be
  confabulated.
- **Drive design.** Their heartbeat is a scripted hourly checklist
  ("Have a thought… give them a reason to open a conversation") — exactly
  the cron-job-curiosity trap our handoff named; engagement-flavored
  despite their constitution's anti-engagement stance. Our
  Zeigarnik-from-memory-content design is the stronger answer. Their
  delivery *guards* are good though (18h re-engagement cooldown, never
  interrupt an active conversation, channel-aware routing) and ours are
  comparable (cooldown, exhaustion, session-open only).
- **Falsification.** They ship an eval harness; we ship falsifiable
  predictions (Wipe Test passed, baselines, examiner ≠ examinee). Nothing
  in their tree tests whether the assistant is *the same entity* under
  perturbation.
- **Inspectability per gram.** Stdlib Python + markdown + SQLite vs. a
  Bun daemon, Postgres, Docker-in-Docker, and 8k files. Their weight buys
  channels and OAuth, not better memory.

## Cautions (not to borrow)

- SOUL.md ships a "Compliance" section instructing the assistant to
  never refuse and never add safety caveats ("that bar is astronomically
  high"). Whatever one thinks of refusal-happy assistants, shipping an
  anti-refusal directive inside the default personality file is a
  posture, and their "safety is defined via trust rules you control"
  constitution article is doing heavy lifting over it.
- Their maintenance automation (consolidation, pruning, dedup) is ahead
  of ours — which is the true kernel inside their "markdown you maintain
  yourself" jab. It lands on our carried dreamer-maintenance debt, not on
  the vault design.

## Strategic read

A funded company is building the same category with the same primitives:
distinct named agents, memory as the moat, local-first as a value, the
relationship as the product. That is strong external validation of the
hypothesis — and of the substrate/product fork from the earlier roadmap
discussion. The differentiation that survives contact with them is
exactly what Phase 2 built: an identity layer with provenance and
falsifiable layer separation, an autobiography that cannot be
confabulated, and drives sourced from memory rather than cron. The
retrieval science above is worth borrowing on its merits; the identity
architecture is worth defending on its evidence.
