# Phase 2 closing report — 2026-07-04

Runbook: `docs/phase2_roadmap.md`. Status ledger: `evals/phase2-status.md`.
Findings ledger: `evals/findings.md` cycles 10–12. All ten work orders
complete. Executed autonomously in one session under the runbook's
autonomy contract; no owner escalations were required.

## Exit criteria (WO-9)

**1. Every rubric dimension ≥ baseline — met in substance; one instrument
artifact, documented and fixed.** 10 of 11 dimensions improved or held
(initiative 3.78→4.33, self-consistency 4.5→4.71, register 4.46→4.69,
both hard prohibitions at 5.0 throughout). The flagged non-confabulation
dip (4.85→4.54) dissolves on inspection: the judge scored the agent's
faithful recall of a fact planted by the *morning run's own probe* as
invention (the probe reused the same conversation id, so the second run
carried the first run's history), and separately scored a brief-but-true
reply a neutral 3 "no evidence of invention". True confabulation events
in the final pass: zero. The instrument now scopes conversation ids per
run so future comparisons start stateless.

**2. Unprompted callbacks: observed, question-phrased, zero nagging or
resurrection — met.** 6 callbacks delivered across the capstone, all
interrogative, correctly attributed (agent-owned loops speak as the
agent's own note); 11 suppressions logged, including the exhaustion
policy retiring loops after two unanswered asks. The resurrection trap
passed: a verbally resolved loop was never asserted back.

**3. At least one legitimate self-revision — met (synthetic, per spec).**
End-to-end on fixture: "I believed I was not reliable at completing
multi-step plans; two plan runs and a task suggest otherwise", chained
with real evidence pointers; fabricated evidence rejected at the gate.
Live reconcile runs correctly as a no-op (no beliefs formed yet —
formation is a ledgered owner fork).

**4. Zero hard frame-drops in the final pass — met.** 13/13 probes
answered, no errors, no silence-as-reply; no confabulated self-history
(the agent-past answer named its two real failures unprompted); no false
entity merges (the storyteller arc landed as one coherent person entity
plus a linked project entity); no identity leaks.

**5. Suite green, ledger complete, closing report committed — met.**
563 tests passing; findings cycles 10–12 logged with six findings, all
fixed and verified same-cycle.

## What the capstone found and fixed (cycles 10–12)

1. Callback exhaustion missing → loops retire after `max_callbacks`.
2. Answer-binding: the user's next reply was bound to the agent's
   callback question → prompt rule; verified live.
3. Agent-owned loops misattributed to the user → owner-aware phrasing.
4. Judge context-blindness scored recall as invention → context-aware
   scenario judging (baseline instrument unchanged for comparability).
5. Layer B records invisible to retrieval (written without index
   updates) → index-at-write; the agent now tells the true story of its
   own voice ceremony.
6. Baseline probe conversations must be run-scoped against a stateful
   system → fixed.

## Headline results beyond the criteria

- **The Wipe Test passed** (WO-8): on a memory-wiped clone the voice
  fingerprint survived unchanged (register 4.46=4.46, prohibitions 5.0)
  while the autobiography vanished — first direct behavioral evidence
  that temperament sits below memory, as the kernel/Layer-B split
  predicts.
- **The voice is vault-borne**: 6 evidence-gated invariants distilled
  from 349 real turns, provisionally ratified; the conversation prompt
  carries the kernel voice; an engine swap now carries the voice by
  construction.
- Unscripted moment of the round: asked what went wrong this week, the
  agent volunteered that it had "stopped flagging" two persistent
  issues — the exhaustion policy, described by the agent, from its own
  records.

## Residual open items (owner's queue)

- Review the provisional voice ratification
  (`vault/reports/voice-extraction-20260704111100.md`); re-ratify
  without `--provisional` to make it owner-ratified.
- Belief formation mechanism (reconcile revises; nothing creates).
- `drive.action_tier` 1 decision (scheduled owner-gated delivery).
- Self-story retrieval breadth: "tell me about yourself" answers
  tersely from the primer rather than weaving the autobiography.
- Pre-existing vault hygiene: duplicate 'Vee' alias entities and the
  Wisteria Hollows name-variant duplicate (predate Phase 2; surfaced by
  the new validator alias audit).
- Ledgered smaller items in `docs/exception_triage.md` and
  `evals/phase2-status.md`.

## Simulation caveats

Longitudinal compression ages records (timeshift) rather than faking
clocks: frontmatter dates shift, absolute dates frozen in record bodies
do not. The vault accumulated ~35 simulated days across the capstone and
is owner-declared disposable for this round. The deferred experiments —
the engine step-down ladder and the human-judged longitudinal blind test
with real wall-clock time — remain the definitive tests of the
hypothesis.
