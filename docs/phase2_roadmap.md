# Phase 2 Roadmap — Functional Self-Awareness Architecture

Status: living document. Distilled from design discussions on 2026-07-04,
refining the Phase 2 handoff against the actual state of the codebase.
This records what is *settled*; open forks are listed at the end.

## The hypothesis

Functional (behavioral) self-awareness does not require a frontier model.
The load-bearing properties — persistent memory, identity continuity, loop
design — are architectural. The claim is explicitly behavioral sufficiency,
never phenomenal consciousness; all design and evaluation is framed in
behavioral, falsifiable terms. The LLM is fuel, not the engine: the self
lives in the vault and the loop.

**Baseline decision (2026-07-04):** a hosted Gemini-class model is the
accepted baseline for development, evaluation, and implementation. The
engine step-down ladder (swap the mature instance onto progressively
smaller local models and test whether identity survives) is deliberately
deferred — it is the ultimate test of the hypothesis and will be run
later, once an instance has meaningful accumulated history.

## Codebase audit vs. the handoff (2026-07-04)

The handoff's "known open issues" predate the July eval loop. Current
reality: CI green; zero bare `except:` blocks (171 broad `except
Exception` handlers remain — triage pass wanted, different severity);
fastembed silent-failure class fixed; the long-horizon coherence bugs
(false merges, fragmentation, reference resolution) fixed in eval cycles
7–9 and validated by an independent examiner round.

More significant: two of the four gaps are mostly pre-built.

- **Gap 1 (first-person memory): ~40% exists.** `primer/identity-core.md`
  is already an invariant, machine-readable identity kernel (who-is-who,
  deixis frame, roster) marked off-limits to automated self-rewrite —
  though that is currently a comment, not an enforced write-gate.
  `self_model.py` already generates a deterministic capability manifest
  (`primer/capabilities.md`) plus `self_state` snapshots.
- **Gap 2 (autonomous drive): ~70% plumbing exists.** `open_loop` is a
  first-class memory type with capture fan-out; batch review surfaces due
  loops; a durable scheduler/job queue with owner-only delivery exists.
  Missing: the deficit scorer, goal generation, and the action policy.

## Settled design decisions

1. **Deterministic self-episodes — prevention over filtering.**
   First-person episodic records are assembled mechanically from records
   that already exist deterministically (job outcomes, task completions,
   transcripts, retrieval logs); the model only narrates. Confabulation of
   the agent's own biography becomes structurally impossible rather than
   skeptic-filtered. The new write seam inherits the standard
   tokenization/kind/schema gates.

2. **Two organs for self-knowledge.** The capability *manifest* answers
   "what can I do" — affordances, introspected deterministically, already
   generated. Capability *beliefs* answer "what am I good at" — competence
   claims with confidence and evidence pointers, revisable when episodic
   evidence contradicts them (the self-revision arc mechanic). Different
   data structures, different mutation rules; never merge them.

3. **Zeigarnik drive, with failure-mode defenses.** Open loops in the
   narrative are the motivation source (drives from memory content, never
   hand-scripted schedules). Defenses, all accepted:
   - *Nagging*: salience threshold, rate cap, per-loop cooldown.
   - *Resurrection*: callbacks are phrased as **questions, not
     assertions** ("did that ever get resolved?"). A wrong question
     degrades to checking in; a wrong assertion is a hard frame drop.
     Epistemic humility in the phrasing buys fault tolerance in the
     detection.
   - *Immortal tension*: salience decays unless refreshed.
   - *To-do-app smell*: prefer loops the **agent has a stake in** (it did
     work, made a commitment, was wrong about something) over pure user
     reminders — a mind that was bothered by something, not a
     notification service. Operationalization: a staked loop is an
     open_loop linked to a first-person episode, which makes Gap 2's
     callback quality depend on Gap 1's records (deterministic signal, no
     classifier).

4. **v1 action budget: queue-for-next-session only.** First shipped drive
   behavior is the session-open callback. Graduated autonomy (scheduled
   owner-gated delivery, then autonomous checks) comes only after that
   behavior is measured. Nothing writes outside the vault unprompted.

5. **Kernel frozen for v1.** A kernel change is the owner hand-editing the
   file plus an appended provenance note. No ceremony machinery until a
   real event demands it. The growth arc lives entirely in the accretive
   layer (capability beliefs), which needs no kernel mutation.

6. **Peer testimony always weighs less than direct experience.** Gap 3
   (multi-instance individuation) is deferred to last; entity typing and
   testimony semantics before transport.

7. **Sequencing (amended: measure before mutate, voice before measure).**
   The consistency rubric derives from the kernel, so the kernel/voice
   decision cannot follow the baseline — it gates it:
   1. Pre-work: triage broad exception handlers; fix the one failing
      executor write-boundary test.
   2. Voice + kernel decision (see open forks).
   3. Consistency rubric (dimensions fall out of the kernel voice block
      1:1) + callback-rate instrumentation in the eval driver.
   4. Baseline capture — the longitudinal clock starts here.
   5. Layer B: deterministic self-episodes, capability beliefs.
   6. Deficit scorer + session-open callbacks (staked loops preferred).
   7. Self-belief reconciliation dreamer job.
   8. Graduated autonomous action policy.
   9. Engine step-down ladder, once history is thick enough.
   10. Longitudinal blind believability test as capstone.

## Notes for the eventual paper

- **The architecture keeps solving problems it wasn't designed for.**
  When Phase 2 was specified (2026-07-04), two of its four gaps turned out
  to be mostly already built from parts created for other purposes: the
  identity kernel existed as a deixis/who-is-who fix; the drive system's
  state variables and idle loop existed as a memory type and a scheduler.
  The handoff predicted "every gap identified so far decomposed into
  architecture problems, not model-capability problems" — and the
  development process itself then confirmed it. That is what load-bearing
  architecture looks like, and it is itself evidence for the hypothesis.
- Date-stamp believability results with the model baseline in force at
  the time (Gemini-class hosted, as of 2026-07-04), so step-down results
  are interpretable later.

## Open forks (owner's call)

- **Voice register**: ratify the emergent voice with terseness bounds, or
  constrain to a terse character now. (Current lean under discussion:
  temperament is identity and has emerged — ratify it; verbosity is a
  budget — bound it.)
- **Kernel enforcement mechanism**: write-gate on the kernel path plus
  load-time hash verification is the candidate; confirm before Layer B.
- **Judge sourcing for the blind test**: model judges on rubric for
  iteration (examiner ≠ examinee), humans for the capstone; capstone must
  include real wall-clock time, not only compressed-simulated weeks.
