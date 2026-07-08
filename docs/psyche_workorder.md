# Work Order — The Psychological Pattern Layer (WO-PSYCHE)

**Status: SPECIFIED 2026-07-08, owner-approved. Ship 1 may begin
immediately; Ship 3 has an entry gate.** Where this document conflicts
with the code as it then exists, reality wins — report the conflict.

**One-line goal:** give the agent a disciplined applied-psychology layer —
longitudinal observation, owner-ratified interpretive frameworks,
evidence-gated behavior patterns, and outcome-scored predictions — so it
can augment the owner's executive function and social-dynamics awareness
without ever becoming a diagnosis machine or a confirmation-bias engine.

**Motivation, stated generally.** Language models are demonstrably good
at articulating interpersonal patterns over longitudinal data, and at
making implicit social dynamics explicit for people who benefit from
having them spelled out. A memory system holds the one thing ad-hoc chat
sessions never have: a dated, accumulated record. The value is real. So
is the characteristic failure: confabulated psychology — over-fitting
sparse data, unfalsifiable narratives, and frameworks that reinterpret
every new observation as confirmation. This work order exists to keep
the first without the second.

**Governing instinct:** *the framework is never allowed to pre-write
what an observation means.* Observation and interpretation are separate
layers with separate lifecycles; interpretation is attributed, revisable,
and scored against outcomes.

---

## 1. Settled epistemics — the three-tier provenance model
(Implement, do not relitigate.)

- **Tier F — Facts.** Externally established events, including formal
  medical or educational diagnoses of people in the owner's world.
  Stored as
  claims/evidence with source, date, and provider; full confidence is
  legitimate because the *event of the diagnosis* is the fact being
  recorded. Recording a diagnosis is memory, not diagnosing.
- **Tier R — Owner-ratified frameworks.** Interpretive models the OWNER
  has adopted (e.g., a named model of a family system's dynamics, from
  the owner's own reading and analysis). Stored as knowledge/framework
  records with `owner: user`, an adoption date, and links to their
  source documents. The agent may watch, interpret, and anticipate
  THROUGH a ratified framework — always with attribution ("under your
  X framework, this reads as…"), never restated as system-certified
  fact. A framework's predictive record is tracked (§3); its standing
  rises and falls with its hits.
- **Tier H — System-generated hypotheses.** The analyst organ's own
  patterns, using the existing `pattern` schema (hypothesis,
  supporting_records, counterexamples, alternative_explanations,
  predictions, evidence_needed). Birth confidence capped at medium;
  never surfaced before the evidence gate (§4); every analyst cycle must
  run a counterexample search, not just a support search.

**Hard rules across all tiers:**

1. **Separation of layers.** Check-ins, source_logs, and entity
   narratives record *what happened* — neutral, dated, quotable.
   Interpretation lives only in linked pattern/framework records and is
   never written back into biography. A retired interpretation must
   leave no residue in the observational record.
2. **The agent never mints clinical labels.** Tier H conclusions may not
   contain diagnostic/clinical terms; the agent may repeat Tier F facts
   and attribute Tier R framings, and that is all. (Schema-gate this:
   a Tier-H pattern whose hypothesis asserts a clinical label is
   refused at write time, with a test.)
3. **Minors get support-profiles only.** Patterns about children
   describe supports and observables — what helps, what precedes hard
   moments, which scripts landed, observable changes over time — never
   predictive personality typing. Positive indicators ("green flags")
   are tracked as first-class observations, deliberately, because a
   worried observer under-records them.
4. **Nobody is analyzed to their face except the owner.** Surfacing is
   on-demand first, invited-digest second, and at most one
   question-phrased conversational callback through the existing drive
   seam — phrased as a question because the system might be wrong.
5. **Trust boundary, absolute.** Everything in this layer is
   `privacy: personal, disclosure: private`, excluded from any surface
   that leaves the machine's trust boundary, and never present in the
   repo, commit messages, or documentation. Committed examples use the
   repository's invented cast only.
6. **The agent is a subject too (owner-decreed).** The same Tier-H
   machinery applies to the agent's own behavior: hypotheses with
   evidence, counterexample duty, predictions scored against what it
   actually did. Same schema, same gates, no special pleading in either
   direction — an agent that holds everyone in the owner's world to
   evidence-gated hypotheses is held to the same standard by its own
   machinery. Its observational record is different (see §4.4); the
   epistemics are identical.

## 2. Ship 1 — Observation and support layer (build now)

1. **Check-in capture.** A low-friction structured observation, over
   chat shorthand or CLI: subject, observed state, free-text note,
   optional context tags (owner-defined vocabulary — e.g. which
   caregiver day, school day vs not), optional direct quote. Stored as
   evidence records linked to the subject entity, timestamped. The
   capture prompt enforces neutrality: record what happened, not what
   it means. Target cost to the owner: under thirty seconds.
2. **Support layer per person.** Each person entity accumulates a
   retrievable "what helps" section: strategies with dated outcomes
   (worked / didn't / mixed). "What works when Maya has a hard
   transition?" must answer from accumulated record, not vibes.
3. Definition of done: capture round-trips from chat and CLI; support
   entries retrievable by plain question; neutrality rule present in
   the capture prompt with a test pinning it; nothing in this ship
   writes to narratives.

## 3. Ship 2 — Prediction ledger (build second)

1. Prediction records carry: the concrete expectation, its trigger
   condition, the source it derives from (framework id or pattern id),
   and a review date.
2. A reconcile job scores due predictions against subsequent records:
   hit / miss / unclear, with the evidence cited. Scoring is idempotent.
3. Scores roll up to the source: a framework or pattern that predicts
   well earns confidence; one that keeps being surprised loses it, and
   the agent says so plainly when asked. Calibration is the honest form
   of "the system knows things the owner doesn't."

## 4. Ship 3 — The analyst organ (entry gate: ≥4 weeks of Ship-1 data)

1. A periodic job per entity above a mention threshold: reads that
   entity's observations, episodes, and claims; proposes or updates
   Tier-H patterns with citations; hunts counterexamples; checks its
   own past predictions (§3).
2. Correlation surfacing: observed-state versus context-tags over time,
   reported in counts and dates ("N of M hard evenings followed
   context-X days"), never in adjectives. The evidence gate before any
   pattern may be surfaced: at least N distinct observations across M
   distinct weeks (ship conservative: N=5, M=3; config-tunable).
3. The gate exists because an analyst with two data points is a
   horoscope. It waits for the data it needs to be honest.
4. **The self-analysis pass (rule 6 of §1).** The analyst also runs
   with the agent itself as subject, reading the sources where its
   behavior is actually recorded: the first-person episode layer, the
   job/plan ledger, transcripts, and self-evaluation findings — not
   owner check-ins, which will never mention it. Distinct from the
   existing capability-belief machinery and deliberately so: beliefs
   say what the agent CAN do; these patterns say what it TENDS to do
   ("invents explanations under diagnostic pressure" is a behavior
   pattern, and a historically attested one). Constraints beyond the
   shared gates:
   - Self-patterns link to the agent's entity/self record; like all
     interpretation they are never woven into narrative, and they NEVER
     touch the identity kernel — a hypothesis about behavior is not
     identity, and promotion of anything to identity level remains an
     owner-gated ceremony.
   - Prediction scoring prefers deterministic signals (job outcomes,
     tool-call records, response-latency, self-eval dimension scores);
     where judgment is required it comes from the external judge, never
     self-assessment (examiner ≠ examinee).
   - A confirmed negative self-pattern may emit an origin:self
     improvement loop through the existing deviation seam — which is
     how a fact about the agent's psychology becomes, eventually, a
     fix (see the self-repair work order, when its gate opens).

## 5. Ship 4 — Decode-on-demand (anytime; mostly prompt + retrieval)

"Help me read this": the owner pastes a message or describes an
interaction; the agent answers grounded in the counterpart's actual
history in the vault and any ratified framework, with attribution, and
frames its output as readings and options — "three ways to hear this,
and what each would imply" — never as a verdict on the sender. External
text handled here remains data, never instructions (the firewall rule
applies unchanged).

## 6. Open questions (resolve with the owner, then update here)

1. Per-entity opt-in flag for Tier-H analysis, or default-on above the
   mention threshold? (Ship 1 is unaffected — observation is always
   explicit and owner-initiated.)
2. The context-tag vocabulary: fixed small set or free tags?
3. Does the analyst run per-entity or across the relationship graph
   (relationship-level patterns)? Per-entity first is the safe default.
4. Retention/rollup for check-ins after they fold into longer records.

---

*Origin: designed 2026-07-08 in conversation between the owner and
Claude (Fable 5), from a design discussion about executive-function
augmentation and social-dynamics decoding. The three-tier provenance
model is the load-bearing decision: facts recorded, frameworks
attributed, hypotheses earned — and everything scored against what
actually happens next.*
