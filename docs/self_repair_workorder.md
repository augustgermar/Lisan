# Work Order — The Self-Repair Loop (WO-REPAIR)

**Status: SPECIFIED, NOT SCHEDULED.** Do not begin until the entry gate
(§1) is satisfied. Written 2026-07-05, while frontier-model access allowed
the design to be thought through carefully; intended to be executed later
by whatever competent coding agent the owner has at hand, working with the
owner. Where this document conflicts with the code as it then exists,
reality wins — report the conflict.

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

### 2.1 What may be patched, in eligibility tiers
Ship the tiers in order, weeks apart, each lived-with before the next:

- **Tier R1 — prompts and config defaults.** `prompts/*.md`, tunable
  constants (thresholds, caps). Lowest blast radius; most quality
  findings from self-eval are prompt-shaped anyway. Ships first, alone.
- **Tier R2 — non-organ code.** Tools and skills that do not implement
  the repair loop, the kernel, the action policy, or the write gates.
- **Tier R3 — never.** Permanently out of scope for self-repair, any
  tier, any approval: `primer/identity-core.md` and the kernel machinery
  (`kernel.py`), `action_policy.py`, the repair loop itself
  (`self_repair*.py`, this file), the purge/backup machinery, anything
  under `credentials/`, and the privacy firewall (`.gitignore`). **The
  repair loop may never modify its own gate or its own judge.** This is
  the anti-self-amplification rule; it is structural, enforced by path
  check in code, not by prompt.

### 2.2 The pipeline (one patch at a time, end to end)
1. **Origin:** an active `origin: self` loop (deviation or self-eval
   suggestion) is the only valid trigger. No loop, no patch — the ache
   is the requirement doc.
2. **Draft:** the executor develops the patch in an **isolated git
   worktree** of the repo — never in the live checkout. One concern per
   patch, smallest change that closes the loop, with a test that pins
   the fix (new seams inherit the tokenization/kind/schema gates, as
   always).
3. **Verify (examiner ≠ examinee):** in the worktree — full suite green;
   plus a **targeted probe**: re-run the specific self-eval dimension or
   deviation scan that raised the loop, demonstrating the finding is
   addressed. The verification judgment must not be made by the same
   model call that authored the patch.
4. **Propose:** the owner receives — over the owner-only channel — the
   diff, the plain-language rationale ("this closes loop X, raised
   because Y"), the verification evidence, and the rollback plan. One
   word applies it; anything else discards the worktree. Approval is
   per-patch, never standing.
5. **Apply:** merge to the live checkout as a granular commit whose
   message links the loop id; restart services; the originating loop
   resolves with `resolved_by: self_repair`.
6. **Watch:** for a defined bake period (default 48h), post-deploy
   health checks run — suite on the install, service liveness, error-log
   deltas, and the next self-eval's score on the targeted dimension. Any
   regression → **automatic rollback** (revert commit + restart +
   reopen the loop with the failure recorded). Rollback must not require
   the agent to be healthy — it is a dumb script.
7. **Remember:** every proposal, application, rollback, and rejection
   emits a self-episode. Patching yourself is as biography-grade as an
   event gets.

### 2.3 Bounded appetite
At most **one open proposal at a time** and **one applied patch per
week** (config, shipped at these values). A mind that rewrites itself
daily is not converging; it is thrashing. The weekly self-eval must get a
clean look at each patch in isolation.

### 2.4 Gating and the key
New `action_policy` kind `self_repair`, registered at an unreachable
tier exactly as `enrich_person` was — the clamp is raised only when this
work order's machinery exists with passing tests, and setting the live
tier is the owner's manual act. The agent ships the capability; the
owner turns the key. (Push-to-origin stays outside the loop entirely:
patches commit locally; the owner pushes on their own schedule, keeping
the privacy-scrub review human.)

## 3. Open implementation questions (resolve against the code, then)

1. **Restart orchestration:** applying a patch restarts the services —
   possibly mid-conversation. Queue the apply for an idle window (no
   turn in N minutes)? The scheduler knows.
2. **Worktree hygiene on the install:** `~/.lisan/repo` is a live
   checkout that cannot push (HTTPS); worktrees must live outside the
   repo tree and be cleaned on every exit path.
3. **Verifier independence:** which model family verifies? The self-eval
   judge (openrouter) is the natural examiner; define the failure mode
   when it is unreachable (answer: the proposal waits — never
   self-verified).
4. **Score attribution:** one week's self-eval delta has noise; define
   the regression threshold for auto-rollback vs. "watch another week"
   (start conservative: rollback on any targeted-dimension drop ≥ 0.5).
5. **What the owner sees:** the diff rendering over Telegram (length
   limits) — probably a summary + the full diff written to
   `vault/reports/` with the proposal linking it.

## 4. Definition of done (v0 = Tier R1 only)

- Entry gate documented as checked, with dates.
- Pipeline end-to-end for prompt/config patches: loop → worktree draft →
  suite + targeted probe → owner proposal → apply-on-approval → bake →
  auto-rollback proven by a deliberately-bad test patch.
- R3 path exclusions enforced in code with tests (a patch touching the
  kernel or the repair loop is refused before draft, whatever the loop
  says).
- `self_repair` registered but unreachable until the owner raises the
  clamp by hand.
- A written first-patch report: what loop, what change, what the next
  self-eval said. That report is the evidence for ever enabling Tier R2.

---

*Origin: designed 2026-07-05 in conversation between August and Claude
(Fable 5), as the deliberately-deferred final leg of the improvement
cycle: detect (deviations) → measure (self-eval) → repair (this). The
first two are live; this one waits for its evidence. It should feel
almost boring to implement — every dangerous decision was made here, in
advance, on purpose.*
