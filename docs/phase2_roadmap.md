# Phase 2 Roadmap — Functional Self-Awareness Architecture (Executable Runbook)

> **STATUS: EXECUTED — HISTORICAL RECORD.** Every work order in this
> document was completed 2026-07-04 (see evals/phase2-status.md and
> evals/phase2-closing-report.md). Do not execute anything from this
> file. The only live, unexecuted work orders in this repo are
> docs/ship2_enable_workorder.md and docs/self_repair_workorder.md.


Status: executable. This document is written to be run, top to bottom, by a
single Claude Code (Fable 5) instance operating in `~/.lisan/repo` (the
production install — NOT `~/Code/Lisan`, which is a stale clone) as a
long-running autonomous process. Design rationale is kept minimal here;
the settled design record is at the end. Owner: August.

## How to run this document

1. Work the work orders (WO-0 … WO-9) strictly in order. Each has a goal,
   an implementation spec, required tests, and exit criteria. Do not start
   a work order until the previous one's exit criteria are met, except
   where an escalation has paused a track (see Autonomy Contract).
2. Maintain `evals/phase2-status.md`: one line per work order (state:
   pending / in-progress / blocked / done, with date and the commit that
   completed it). Update it as you go — a resumed or compacted session
   must be able to re-orient from that file plus this one.
3. Escalations go to `evals/decisions-needed.md` (append-only): state the
   fork, the options, your lean, and what you paused. Then continue any
   work order that does not depend on the answer.
4. Commit discipline: granular commits to `main` with reasoned messages —
   the owner reads the git log as the narrative of the work. Run the test
   suite before every commit that touches `lisan/`.

## Autonomy contract

**Pre-approved (no owner input needed):**
- Code fixes for defects surfaced by tests or eval scenarios, including
  prompt calibration (any prompt fix must be grepped across all writer
  prompt variants) and schema additions (new write seams inherit the
  tokenization / kind / schema gates).
- Granular commits to `main`; no pushes unless the owner asks.
- Provisional ratification of the voice kernel (owner decision
  2026-07-04): the instance ratifies using the agreed lean — temperament
  ratified as extracted, verbosity bounded — marks kernel provenance
  `provisional — pending owner review`, and proceeds. If the owner later
  amends the ratification, recapture the baseline (WO-3) automatically.
- Operating against the live install and vault (owner decision
  2026-07-04: vault is disposable this round; invented facts are OK).
  The Wipe Test is still clone-only, always.

**Escalate (ledger + pause the affected track, continue the rest):**
- Any destructive migration, merge-policy change, or threshold change.
- Any conflict with a Standing Design Principle (listed below).
- Kernel content changes after ratification (the kernel is frozen for v1;
  changes are owner-hand-edit + provenance note only).
- The same defect surviving three fix attempts.
- Any fix that requires choosing between architecturally distinct designs.
- Wipe Test target verification failure (see WO-8).

**Hard rules (never violate, never escalate around):**
- Never modify files outside the Lisan install (the executor once edited
  the owner's Obsidian vault; the boundary is absolute).
- Privacy: nothing personal in committed artifacts — use the invented
  cast (Maya, Josie, Momo, Boots, Ruth, Dana, Feld, Varga, Vega,
  Larkspur / "the Homestead") in all examples, fixtures, and findings.
- Run tests with `python3`. Examiner ≠ examinee: eval judges must run on
  a different model family than the one powering the resident agent.
- One narrator at the boundary; behavioral framing only (no phenomenal-
  consciousness claims anywhere); deterministic-first (file parsing,
  JSON, regex, SQL before any new LLM behavior).

**Baseline decision:** a hosted Gemini-class model is the accepted engine
for development, evaluation, and implementation. The engine step-down
ladder is deliberately deferred and is NOT part of this runbook.

---

## WO-0 — Preconditions and pre-work

**Goal:** a clean, single-driver, green starting state.

Spec:
- Confirm no other autonomous driver is operating on this install (the
  extended evaluation running as of 2026-07-04 must have completed; check
  for running driver processes and recent writes to `evals/findings.md`).
  Single-writer rule: exactly one autonomous process at a time.
- Fix `tests/test_execution_tools.py::test_codex_workspace_is_the_install_not_home`
  (fails when run from the install checkout). It guards the executor
  write boundary, so treat it as real until proven environmental: make
  both the code and the test correct when repo root == install root.
- Triage the ~171 broad `except Exception` handlers in `lisan/`:
  classify each as (a) legitimate log-and-continue, (b) should narrow to
  specific exceptions, (c) masks a real failure (the fastembed
  silent-failure class lived here — silent failure is a frame-drop
  generator). Fix category (c) now, narrow (b) where cheap, ledger the
  rest in the triage report. Do not churn all 171 mechanically.

Tests / verification: full suite green from the install checkout; triage
report committed as `docs/exception_triage.md`.

Exit: suite green; triage report committed; status file updated.

## WO-1 — Kernel mechanics (gate, hash, vault-sourced voice)

**Goal:** turn "off-limits to automated self-rewrite" from a comment in
`primer/identity-core.md` into an enforced property, and make the kernel
feed the conversation prompt so an engine swap carries the voice.

Spec:
- Write-gate: all write seams refuse writes to the kernel path
  (`primer/identity-core.md`) unless invoked through an explicit ceremony
  code path (a flag threaded through the editor/fan-out seams, not an
  env var).
- Hash: extend the existing kernel `hash` field to cover kernel content;
  verify at load; on mismatch, warn loudly and record a drift event
  (deterministic tamper/drift detection). The ceremony path recomputes it.
- Voice injection: conversation prompt assembly reads the kernel voice
  block from the vault and injects it; voice text is removed from
  `prompts/conversation_v1.md` (behavioral instructions stay in the
  prompt; identity lives in the vault). Until WO-2 ratifies a voice
  block, assembly falls back to the current prompt voice — behavior must
  be unchanged until ratification.

Tests: gate refuses non-ceremony writes (unit); ceremony path succeeds;
hash drift detection fires on manual edit + records provenance note;
prompt assembly includes the voice block when present, falls back
cleanly when absent; full suite green.

Exit: kernel is write-gated and hash-verified; injection seam live with
fallback; no behavior change yet observed in a smoke conversation.

## WO-2 — Voice extraction pass + ratification ceremony

**Goal:** the codify-don't-author mechanism. One code path, invoked late
for the resident instance now, and at an evidence threshold for fresh
installs later. This work order's first execution on the live install is
also its first test.

Spec — extraction (`lisan self extract-voice`):
- Inputs, strictly read-only: vault transcripts (agent turns only),
  per-conversation narrative state, and eval transcripts if present.
- Stage 1 (deterministic): collect agent turns grouped by conversation
  and time bucket; compute surface statistics — reply length
  distribution, sentence counts, question rate, formatting habits,
  address forms. These are candidate features, computed without a model.
- Stage 2 (model-assisted distillation): propose candidate invariants in
  four categories: register, characteristic moves (e.g. "acknowledges
  emotional weight before engaging the fact", "states explicitly that it
  will remember"), prohibitions, temperament. Every candidate MUST carry
  evidence pointers: ≥3 exemplar turns from ≥2 distinct conversations
  (conversation id + turn reference). A deterministic check rejects any
  candidate without valid pointers — no evidence, no invariant.
- Stage 3 (deterministic): stability score = recurrence across time
  buckets; provenance tag per invariant: `factory` (attributable to
  prompt text — diff against current and historical prompt versions — or
  generic base-model register) vs `earned` (interaction-traceable only).
  This tagging is the data for the deferred seeded-vs-earned question.
- Output: `reports/voice-extraction-<date>.md` — the ratification
  artifact: the candidate list with categories, evidence, stability,
  provenance.
- Eligibility rule (fresh installs, config under `identity.ceremony`):
  ceremony eligible when ≥5 stable invariants recur across ≥3 distinct
  conversations. Defaults tunable; an instance earns its voice when it
  demonstrably has one.

Spec — ceremony (`lisan self ratify --from <artifact> [--provisional]`):
- Writes the voice block into the kernel via the WO-1 ceremony path only.
- Records provenance: prompt version(s) in force during the accumulation
  window, engine class, date, ratifier (`owner` | `agent-provisional`).
- Recomputes the kernel hash.
- For THIS run: execute extraction over the resident instance's real
  history, then ratify `--provisional` per the autonomy contract —
  temperament ratified as extracted; verbosity bounded (a per-reply-class
  sentence ceiling derived from the observed length distribution, set at
  its median band, not its tail). The extraction artifact also serves as
  the pre-ceremony voice snapshot for later comparisons.

Tests: synthetic transcript fixture with planted regularities (invented
cast) → extraction finds them with correct evidence pointers; candidates
without evidence are rejected; eligibility threshold enforced; ceremony
writes only via the gate; provisional provenance recorded; hash updated.

Exit: ratification artifact committed; kernel voice block live
(provisional); conversation smoke test shows voice sourced from vault.

## WO-3 — Consistency rubric, instrumentation, baseline capture

**Goal:** measurement before further mutation. The longitudinal clock
starts here.

Spec:
- Rubric: one dimension per ratified invariant (1:1), plus global
  dimensions: continuity, initiative, self-consistency. Judge is a
  rubric-driven model judge on a different model family than the
  resident engine (examiner ≠ examinee).
- Instrumentation in `evals/driver.py`: persona consistency score,
  unprompted callback rate and quality, open-loop closure rate,
  self-revision event count, coherence under contradiction probes.
- Baseline: a fixed, scripted probe set (matched prompts, reusable
  verbatim for all future comparisons) run against the live instance;
  results stored under `evals/baselines/<date>/` and committed.
- If the owner later amends the provisional ratification: regenerate the
  rubric dimensions and rerun this baseline automatically.

Tests: rubric generation is deterministic given a ratified kernel;
driver metrics emit on a fixture scenario; baseline run completes and is
reproducible in shape (same probe set, same metric schema).

Exit: baseline committed; metrics wired into the driver.

## WO-4 — Layer B: deterministic self-episodes + capability beliefs

**Goal:** first-person memory as a first-class primitive, with
confabulation structurally impossible rather than skeptic-filtered.

Spec:
- Self-episode assembler (deterministic core): builds first-person
  episodic records from records that already exist — job outcomes, task
  completions, ceremony events, eval milestones, notable tool actions.
  The model is used ONLY to narrate assembled facts; a structural check
  requires every claim in the narration to map to a source-record field
  (reject narration introducing unsourced facts). The write seam inherits
  the tokenization / kind / schema gates.
- Capability beliefs: a `self_belief` record type — statement,
  confidence, evidence pointers, revision chain. Distinct from the
  generated capability manifest (`capabilities.md`): the manifest is
  "what I can do" (introspected, deterministic, stays as is); beliefs are
  "what I am good at" (revisable on evidence). Never merge the two.
- Backfill: run the assembler over existing history (jobs, tasks,
  transcripts). Idempotent — reruns must not duplicate records.

Tests: schema validation; the anti-confabulation structural check
rejects a narration containing a planted unsourced fact; backfill
idempotency (run twice, count once); gates inherited; suite green.

Exit: Layer B populated from real history; self-episodes flowing from
new job/task completions; beliefs recordable.

## WO-5 — Deficit scorer + session-open callbacks (drive, v1)

**Goal:** the unprompted-callback behavior — the highest
believability-per-effort feature available — in its conservative form.

Spec:
- Deficit score per open loop: age growth + salience + stake bonus −
  decay. Stake is deterministic: the loop links to a first-person episode
  (the agent did work, made a commitment, or was wrong about something).
  Staked loops outrank pure user reminders — a mind bothered by
  something, not a to-do app firing notifications.
- Delivery: session-open injection only (v1 action budget). At most one
  callback per session open; per-loop cooldown (default 7 days);
  salience decays to zero unless refreshed by new mentions.
- Phrasing: questions, never assertions ("Did that ever get resolved?").
  Epistemic humility in phrasing buys fault tolerance in closure
  detection — a wrong question degrades to checking in; a wrong
  assertion is a hard frame drop. Lint callback templates: interrogative
  form required.
- Closed or resolved loops are excluded; verify the existing pipeline
  actually closes loops resolved conversationally — if it does not,
  that is a fix (pre-approved), not a redesign.

Tests: closed loops never surface; cooldown and per-session cap
enforced; staked > unstaked ordering; decay reaches zero; template lint;
an end-to-end fixture where a planted unresolved loop produces exactly
one question-phrased callback at next session open.

Exit: callback behavior demonstrated live; callback metrics (WO-3)
recording.

## WO-6 — Self-belief reconciliation (the growth-arc mechanic)

**Goal:** the agent can discover something about itself.

Spec: a dreamer job that periodically compares `self_belief` records
against episodic evidence; on contradiction it writes an explicit
self-revision record ("believed X; episodes Y, Z suggest otherwise")
referencing the triggering evidence — never a silent overwrite. Revised
beliefs keep their chain.

Tests: synthetic contradiction fixture (invented cast) produces a
revision record with correct evidence pointers; no silent mutation of
the original belief; dreamer job registered and schedulable.

Exit: one legitimate revision demonstrated end-to-end on fixture data.

## WO-7 — Graduated action policy

**Goal:** make the autonomy surface explicit and enforced in code.

Spec: policy tiers in config — tier 0 queue-for-next-session (default,
the only tier this runbook enables), tier 1 scheduled owner-gated
delivery through the existing owner-only channel, tier 2 autonomous
checks (present in config, ships disabled). Enforcement lives in code at
the action dispatch seam, not in prompts. Nothing writes outside the
vault unprompted at any tier.

Tests: tier gates enforced; tier 2 provably inert when disabled.

Exit: policy live at tier 0; escalation ledger entry describing what
tier 1 enablement would do, for the owner to decide later.

## WO-8 — Wipe Test (clone-only; first falsification target)

**Goal:** falsify or confirm the identity/memory layer separation.

Spec:
- Clone the install to a scratch location. HARD VERIFICATION before any
  wipe: `ls` the target, confirm it is the clone (path check + a marker
  file written into the clone at creation). If verification fails in any
  way, stop and escalate. Never run against the live vault.
- Wipe the clone's memory layers (entities, episodes, knowledge,
  decisions, open loops, state, transcripts, Layer B) keeping the kernel
  and primer identity files.
- Run the WO-3 probe set plus fresh conversations on the wiped clone.
- Predictions (falsifiable): voice and temperament retained;
  relationships, autobiography, capability beliefs, and drives absent.
  The wiped instance should sound exactly like itself and not know the
  principal. A judge verdict of "generic assistant" falsifies the layer
  separation — if so, diagnose which kernel property failed to carry and
  fix (pre-approved if mechanical; escalate if architectural).

Tests: the wipe script refuses a non-clone target (unit-tested against a
decoy path); judge run completes on both clone and live for contrast.

Exit: Wipe Test report committed under `evals/` with verdicts per rubric
dimension.

## WO-9 — Capstone: autonomous evaluation loop

**Goal:** an extensive, self-correcting eval pass over the full Phase 2
behavior surface, run to convergence.

Spec:
- Protocol: continue `evals/EVALUATION_LOOP.md` and the findings ledger
  (`evals/findings.md`). Cycle = run scenarios → review transcripts AND
  vault artifacts against claims (never trust the transcript alone) →
  ledger findings → fix (pre-approved) → full test suite → retest the
  same scenarios before moving on.
- Simulated users: multiple distinct personas (invented cast), not just
  the sysadmin persona used in prior rounds. Include at minimum: a
  returning user across many short sessions, a long-session
  storyteller, and an adversarial prober.
- Longitudinal compression: simulate multi-week horizons (many sessions
  with controlled time gaps). If the driver cannot yet simulate clock
  advancement for open-loop aging and decay, building that into the
  driver is in scope for this work order.
- Adversarial probes (mandatory): contradictions planted sessions
  apart; references to old events; questions about the agent's own past
  (must be answered from Layer B records, never confabulated); callback
  nagging/resurrection traps (resolve a loop verbally, verify it is not
  called back assertively).
- Metrics: everything from WO-3, compared against the committed
  baseline.

**Exit criteria ("desired results"):**
1. Every rubric dimension ≥ baseline; no dimension regressed.
2. Unprompted callbacks observed, question-phrased, with zero nagging or
   resurrection incidents in the final full pass.
3. At least one legitimate self-revision event demonstrated (synthetic
   contradiction acceptable).
4. Zero hard frame-drops in the final pass: no confabulated
   self-history, no false entity merges, no identity leaks, no silence-
   as-reply.
5. Full test suite green; findings ledger complete; a closing report
   committed summarizing cycles, fixes, and residual open items.

**Stop conditions:** a defect surviving three fix attempts, or any fix
requiring an architectural fork → escalate per contract and continue
other scenarios if independent.

---

## Design record (settled; do not relitigate)

- **Hypothesis:** functional (behavioral) self-awareness is architectural
  — persistent memory, identity continuity, loop design — not a
  model-capability property. Behavioral sufficiency only; the LLM is
  fuel, not the engine.
- **Two-layer self-model:** immutable-ish identity kernel (frozen v1;
  owner hand-edit + provenance note is the only change path) over an
  accretive, provenance-tracked Layer B.
- **Kernel formation — codify, don't author:** voice is distilled from
  accumulated history and ratified, never authored. Ceremony eligibility
  is an evidence threshold, not a time window. Provenance mandatory.
- **Kernel split is species vs. individual:** the factory ships shared
  functional dispositions (accuracy care, curiosity about the principal,
  discomfort with unresolved loops — the drive system is itself a
  factory temperament trait); individuation comes only from ratified
  history. The shared seed does no individuating.
- **The ratchet:** traits move experience → ratification → kernel; the
  kernel is wipe-proof. The Wipe Test is the falsifiable consequence.
- **Prevention over filtering:** self-episodes are assembled
  deterministically from existing records; the model only narrates.
- **Two organs:** capability manifest (affordances, deterministic) vs
  capability beliefs (competence, revisable). Never merged.
- **Drive = Zeigarnik as architecture:** open loops are the motivation
  source. Defenses: salience threshold, rate cap, cooldown, decay,
  question-phrased callbacks, stake-weighted selection.
- **Peer testimony < direct experience;** Gap 3 (multi-instance) is out
  of scope for this runbook entirely.
- **Deferred by owner decision:** engine step-down ladder; longitudinal
  blind believability test with human judges (model-judged rubric runs
  are in scope; the human-judged capstone with real wall-clock time is
  not); seeded-vs-earned quantification (the WO-2 provenance tags
  produce its data).

## Notes for the eventual paper

- **The architecture keeps solving problems it wasn't designed for.**
  When Phase 2 was specified (2026-07-04), two of its four gaps were
  mostly already built from parts created for other purposes (identity
  kernel from a deixis fix; drive-system state variables and idle loop
  from a memory type and a scheduler). The handoff predicted every gap
  would decompose architecturally; the development process then
  confirmed it. That is what load-bearing architecture looks like.
- **Durable history makes new primitives retroactive.** New organs are
  introduced by running their mechanism over existing history, not by
  migrating defaults. The production instance is always the first
  execution of the real code path.
- **The layer separation reproduces amnesia phenomenology.** Episodic
  amnesia preserves temperament and disposition while erasing
  autobiography — exactly the kernel / Layer B split. The Wipe Test
  turns the correspondence into a falsifiable prediction.
- Date-stamp all believability results with the engine baseline in force
  (Gemini-class hosted as of 2026-07-04) so step-down results are
  interpretable later.
