# Work Order — The Self-Repair Loop (WO-REPAIR)

**Status: PHASE A/B SHIPPED 2026-08-16; PHASE C SHIPPED 2026-08-19.** The entry
gate was reviewed, Phase A was lived through an owner-approved proposal, Phase B
was exercised against the live checkout, and Phase C was proven with a
deliberately bad disposable patch (full lifecycle test: apply, detect regression,
rollback, reopen loop, emit episode). Written 2026-07-05; where this document
conflicts with the code as it then exists, reality wins — report the conflict.

**One-line goal:** close the last gap in the improvement cycle — the agent
already *detects* its own defects (deviation drive) and *measures* its own
quality (self-evaluation organ); this work order lets a finding graduate
into a proposed, verified, owner-approved patch to the agent's own code,
with automatic rollback. The agent diagnoses and drafts the treatment; the
owner remains the physician of record.

**Governing instinct:** a self-patching agent is the capability that
punishes sloppy foundations hardest. Everything here is shaped by one
asymmetry — a bad patch doesn't break a feature, it breaks the running
mind, possibly including its ability to fix itself. Slow is smooth.

---

## 1. Entry gate (all required before work begins)

- At least **4 weeks of self-eval history** in
  `vault/reports/self-eval-history.jsonl` — "did the patch make me
  better?" needs a baseline to be answerable, and the verifier (§4) is
  built on that history.
- The WO-ENRICH **calibration read** (mosquito-vs-indexer) is written and
  the deviation drive has been lived with at real volume.
- The test suite is green and has been green in CI for the intervening
  period (the suite is the floor of every verification below).
- The owner affirmatively wants this. Re-read this document together
  first.

## 2. Settled policy (implement, do not relitigate)

### 2.1 What may be patched
There is no artificial R1/R2 rollout limit. From the first release, the
agent may propose improvements to ordinary application behavior, including
prompts, configuration, tools, providers, retrieval, transcript search,
enrichment, jobs, and other non-organ code. A proposal still has to be
small, testable, attributable to one active self-loop, and approved by the
owner before it reaches the live checkout.

The following are permanent protected boundaries, regardless of proposal
quality or owner approval:

- `primer/identity-core.md` and the kernel machinery (`kernel.py`)
- `action_policy.py` and any policy clamp or capability gate
- the self-repair implementation, its verifier, and this work order
- purge, backup, and rollback machinery
- anything under `credentials/`
- the privacy firewall and repository-boundary controls, including
  `.gitignore`

The repair loop may never modify the mechanisms that decide what it may
change, how a change is verified, or where it may write. This is the
anti-self-amplification rule; it must be enforced structurally by path and
capability checks, not by prompt instructions.

### 2.2 The pipeline (one patch at a time, end to end)
1. **Origin:** an active `origin: self` loop (deviation or self-eval
   suggestion) is the only valid trigger. No loop, no patch — the ache
   is the requirement doc.
2. **Draft:** the executor develops the patch in an **isolated git
   worktree** of the repo — never in the live checkout. The live checkout
   must be clean before a proposal is created; self-repair must refuse to
   overwrite, stash, reset, or otherwise disturb owner changes. One concern
   per patch, smallest change that closes the loop, with a test that pins
   the fix (new seams inherit the tokenization/kind/schema gates, as always).
3. **Verify (examiner ≠ examinee):** in the worktree — full suite green;
   plus a **targeted probe**: re-run the specific self-eval dimension or
   deviation scan that raised the loop, demonstrating the finding is
   addressed. The verification judgment must not be made by the same
   model call that authored the patch.
4. **Propose:** the owner receives a compact proposal over the owner-only
   channel. Telegram is the initial transport, using the existing
   allowlist-locked bot and confirmation records. The message includes the
   proposal id, exact file list, rationale, verification evidence, risk,
   rollback reference, and the full report/diff path in the vault. Approval
   is an explicit `approve <proposal-id>` or `deny <proposal-id>` for the
   exact proposal hash; it is never standing approval. Expired or changed
   proposals cannot be applied.
5. **Apply:** only after approval, verify that the live checkout is still
   the expected clean base and that the proposal hash is unchanged. Apply
   the isolated worktree as one granular commit whose message links the
   loop and proposal ids. Queue service restart for a safe idle window;
   never interrupt an active conversation. The originating loop resolves
   only after successful application, with `resolved_by: self_repair`.
6. **Watch:** for a defined bake period (default 48h), a dumb monitor runs
   outside the agent — suite on the install, service liveness, error-log
   deltas, and the next self-eval's score on the targeted dimension. Any
   regression reopens the loop and invokes the recorded rollback artifact.
   Rollback must identify the exact applied commit, verify that no later
   owner commit has made automatic reversal unsafe, restore the known-good
   state, and restart services if necessary. It must not depend on the
   agent being healthy.
7. **Remember:** every proposal, application, rollback, and rejection
   emits a self-episode. Patching yourself is as biography-grade as an
   event gets.

### 2.3 Bounded appetite
At most **one open proposal at a time** and **one applied patch per
week** (config, shipped at these values). A mind that rewrites itself
daily is not converging; it is thrashing. The weekly self-eval must get a
clean look at each patch in isolation.

### 2.4 Gating and the key
New `action_policy` kinds `self_repair_propose` and `self_repair_apply` are
registered separately. Phase A is reachable at tier 3; Phase B requires tier
4 and was raised by the owner on 2026-08-16 after the passing implementation
and regression suite. The tier remains the owner's manual act. Push-to-origin
stays outside the loop entirely: patches commit locally; the owner pushes on
their own schedule, keeping the privacy-scrub review human.

### 2.5 Implementation phases

The phases separate increasingly consequential actions. They are not
eligibility tiers for which files may be improved.

- **Phase A — propose and verify, no apply.** Detect an eligible self-loop,
  create an isolated worktree, draft the smallest ordinary-code change,
  run the full suite and targeted probe with an independent verifier, and
  send the owner a Telegram confirmation proposal. The live checkout is
  never changed.
- **Phase B — owner-approved apply — SHIPPED and exercised 2026-08-16.**
  After an exact approval, apply the verified worktree as a granular local
  commit, record the proposal and approval, resolve the originating loop, and
  queue a safe service restart.
- **Phase C — bake and rollback — SHIPPED 2026-08-19.** Monitor the applied
  commit for a bake period (default 48h) via self-rescheduling
  `self_repair.bake_check` jobs. Four probes: test suite, service liveness,
  error-log delta, targeted self-eval dimension score. Regression rule: suite
  failure or ≥0.5 drop on the targeted dimension. Inconclusive (no self-eval
  yet) extends the bake period up to 2 times. Rollback is `git revert` of the
  exact applied commit — refused if owner commits sit on top. Reopens the
  origin loop. Policy kind `self_repair_rollback` at tier 4 (co-gated with
  apply). Proven with `test_full_bake_regression_lifecycle`: a deliberately bad
  patch applied, bake check catches the suite failure, rollback fires, file
  restored, loop reopened, episode emitted.

Phase A was lived with before Phase B was enabled. Phase C was proven with a
deliberately bad disposable patch in the test suite before it was shipped.

## 3. Open implementation questions (resolve against the code, then)

1. **Restart orchestration:** define the idle-window threshold, the queue
   owner, and the recovery behavior if a service does not restart cleanly.
2. **Worktree hygiene on the install:** the installation checkout is a
   live checkout that may not be able to push; worktrees must live outside
   the repo tree and be cleaned on every exit path.
3. **Verifier independence:** the verifier must use a model family or
   deterministic harness independent of the author. The existing external
   self-eval judge is the default candidate. If it is unavailable, the
   proposal waits; it is never self-verified.
4. **Score attribution:** define the regression rule for the targeted
   metric. The initial conservative rule is a drop of at least 0.5 on the
   targeted dimension, while allowing a documented inconclusive result to
   extend the bake period rather than triggering a speculative rollback.
5. **What the owner sees:** Telegram carries a bounded summary and exact
   approval command; the complete diff, report, verifier output, proposal
   hash, and rollback metadata live under `vault/reports/`.
6. **Config eligibility:** define an allowlist of configuration keys that
   ordinary self-repair may change. Policy tiers, credentials, privacy,
   write boundaries, model-authority settings, and rollback settings remain
   protected even when stored in ordinary config files.

## 4. Definition of done (v0 = Phase A/B, broad ordinary-code scope)

- Entry gate documented as checked, with dates.
- Phase A is end-to-end for an ordinary non-organ code patch: self-loop →
  isolated worktree draft → full suite + targeted probe → independent
  verification → Telegram proposal → owner approval record, with no live
  checkout mutation.
- R3 path exclusions enforced in code with tests (a patch touching the
  kernel or the repair loop is refused before draft, whatever the loop
  says).
- `self_repair_propose` and `self_repair_apply` are registered separately;
  apply is reachable only after the owner raises the tier-4 clamp by hand.
- Dirty-checkout refusal, proposal hashing, approval binding, verifier
  unavailability, worktree cleanup, and path exclusions are test-covered.
- A written first proposal report exists, including the originating loop,
  proposed change, verifier result, owner decision, and rollback metadata.

Phase B additionally requires a clean-base apply and safe restart test —
completed 2026-08-16 with local commit `1e59bd5` and a controlled service
restart.
Phase C additionally requires a proven rollback against a deliberately bad
disposable patch and a documented bake result — completed 2026-08-19 with
`test_full_bake_regression_lifecycle` (22 self-repair tests total, all passing).

---

*Origin: designed 2026-07-05 in conversation between August and Claude
(Fable 5), as the deliberately-deferred final leg of the improvement
cycle: detect (deviations) → measure (self-eval) → repair (this). Detection,
measurement, and Phase A/B repair are now live; Phase C remains deliberately
deferred until bake and rollback are proven. It should feel almost boring to
implement — every dangerous decision was made here, in advance, on purpose.*
