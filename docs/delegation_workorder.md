# Delegation work order

Status: **steps 1-5 built and trialled (2026-10-02).** Still to do: restart the
Telegram bot and watch it with the lanes and the `delegate` tool; Phase 4 (the
learning loop) builds on the briefs and results this layer keeps.
Author: Claude (Sonnet 5.5) with August, 2026-10-02. Follows the
execution-hardening branch (`fc2eacd`, `a7aa7d5`).

## Problem

Lisan's only worker is one `codex exec` subprocess, run one at a time, inside
whatever job or chat turn asked for it. That has three consequences that
matter now that it does real work:

1. **No parallelism.** "Audit these five servers" is five sequential steps.
2. **Head-of-line blocking.** The scheduler loop (`scheduler.py:run_scheduler_loop`
   → `run_jobs_worker`) drains the whole queue serially. A 30-minute codex
   plan step holds every reminder and background job behind it. Delegation on
   the same lane would make this worse, so lane isolation is part of the work.
3. **No scoped authority.** Every codex run gets the executor's sandbox. There
   is no way to say "this child may read, not write".

Hermes solves (1) and (3) with `delegate_task`: a fresh agent per child,
isolated context, 3 concurrent, depth 1, restricted tools, a timeout. We want
that shape, but durable: Hermes loses a child on a crash; Lisan's queue should
not.

## What already helps (verified in code)

- `claim_next_job` claims with `BEGIN IMMEDIATE` (`jobs.py:579`). **Several
  workers can already share one queue safely.** The gap is who starts them.
- `claim_next_job(job_types=…)` and `run_jobs_worker(job_types=…, max_jobs=…)`
  already support lane-restricted workers.
- `JobDeferred` / `defer_job` reschedule without spending an attempt, bounded
  by `MAX_DEFERRALS`.
- `enqueue_job(coalesce_key=…)` merges duplicates of a queued/running job.
- The shared run ledger (`run_ledger.py`) takes a new `origin`.
- `adjutant_reporter.report_result` is the one front door back into memory.

## Design

### 1. A child is a job: `agent.delegate`

One job row per child. Payload:

| field | meaning |
|---|---|
| `delegation_id` | stable id; `group_id` groups siblings of one fan-out |
| `brief` | the child's whole task. The child sees nothing else |
| `profile` | `read_only` \| `workspace_write` \| `full` (see §2) |
| `working_directory` | where it runs |
| `timeout_seconds` | clamped to ≤ 2400 (see Constraints) |
| `result_schema` | optional JSON schema; the child must answer in it |
| `parent` | `{kind: plan\|chat\|adjutant, ref: …}` for provenance |

The handler runs `CodexClient.complete(...)` with the profile's sandbox, the
timeout from Phase 1, one ledger row (`origin="delegate"`), and returns
`{status, text|data, duration_s, artifacts}`. A child cannot delegate (depth
1 by construction: it is plain `codex exec` with no Lisan tools), and cannot
read memory. Context isolation is the default, not a feature to build.

### 2. Scoped authority

`CodexClient.complete()` gains an explicit `sandbox_mode` override, which wins
over the config precedence in `_resolve_sandbox_mode`. Profiles map to codex
modes: `read_only` → `read-only`, `workspace_write` → `workspace-write`,
`full` → bypass. **A child's profile may never exceed its parent's** (a
read-only plan cannot spawn a `full` child), and every child passes the same
`intent.md` never-rule check `run_codex` and plan steps use. `full` requires
the delegating caller to already hold the executor's authority; chat-minted
delegations default to `read_only`.

### 3. Lanes: stop long work blocking short work

Replace the single drain in the scheduler loop with two lanes:

- **main lane**: everything except `agent.delegate`, as today.
- **delegate pool**: up to `delegation.max_concurrent` (default 3) worker
  threads, each `run_jobs_worker(job_types={"agent.delegate"}, max_jobs=1)` in
  a loop. Threads are enough because a child spends its time waiting on a
  `codex` subprocess; the send-function thread-local is already per-thread.

Side benefit: the main lane no longer stalls behind long codex work at all if
`task.run_codex` and `plan.run` later move to the pool too. That is a separate,
optional step.

### 4. Fan-out and join, durably

New plan step kind `fanout`: `{children: [{brief, profile, …}, …], join: "all"|"best_effort"}`.

1. The step job enqueues N child jobs (cap `delegation.max_children`, default 6)
   sharing a `group_id`, records the ids in the step, and ends **without**
   advancing the plan (new step status `waiting`).
2. When a child reaches a terminal state, a hook checks its group under
   `BEGIN IMMEDIATE`; the last one enqueues the plan's continuation `plan.run`
   (coalesce key `<plan_id>:join:<step>` so it cannot double-fire).
3. A sweep on every scheduler tick enqueues the continuation for any group
   whose children are all terminal but whose step is still `waiting`. This
   makes the join crash-safe: the hook is an optimization, the sweep is the
   guarantee.
4. The continuation collects child results into the step result. `all`: any
   failed child fails the step (and the plan, with the existing resume path).
   `best_effort`: failed children are listed as failed and the plan continues.

Children are never auto-retried (same rule as timed-out plan steps: ambiguous
side effects). A failed child shows up in `lisan plan resume` like any step.

### 5. Chat and Adjutant entry points

- **Chat**: a `delegate` tool, **asynchronous only**. It returns a handle
  immediately ("delegated, I'll report back"); it never blocks the turn. When
  the group finishes, one capture turn reports the whole group (not one per
  child, which would flood memory) and the owner gets the usual delivery.
- **Adjutant**: out of scope for v1. A task kind `delegate` is a natural later
  addition, gated by `intent.md` capabilities like the others.

### 6. Visibility and control

`lisan delegate run|list|show`, delegation rows in `lisan self state`
(active groups, per-child status), and `cancel_plan` cancels queued children.
**Running children are not killed in v1** — they are bounded by their timeout.
Killing needs the child's pid recorded on the job; listed under Open questions.

## Constraints and traps (found in the code)

- **Child timeout must stay under the 45-minute stale-job reclaim**
  (`reclaim_stale_running_jobs`, `jobs.py:1684`), or a live child is requeued
  and runs twice. Hence the 2400s clamp. Any longer task should be a plan of
  children, not one child.
- **Memory pollution.** One capture turn per *group*, not per child.
- **Recursion.** Children have no Lisan tools, so the 2026-07-27 plan-recursion
  failure mode cannot recur through delegation; `_inside_a_plan` still guards
  the plan tool.
- **Cost.** There is no per-run cost accounting anywhere in Lisan today. The
  bounds in v1 are counts (`max_children`, `max_concurrent`, outstanding cap)
  and wall time, not tokens.
- **macOS sleep.** `hold_awake` is per job already; the pool inherits it.

## Build order (each step shippable, suite green between)

1. `CodexClient.complete(sandbox_mode=…)` override + profile mapping + tests.
2. `agent.delegate` job type: handler, ledger, gates, budgets, structured
   results, CLI. Tested with a fake codex, as in `test_codex_timeout.py`.
3. Delegate pool in the scheduler loop; test that a long main-lane job does
   not delay a delegate and vice versa.
4. Plan `fanout` step: join hook + sweep + resume interplay.
5. Chat `delegate` tool and group-level capture report.

Steps 1–3 deliver parallel, scoped, durable single-delegation. Steps 4–5 add
orchestration. I would stop after 3 for a real-world trial before building 4–5.

## Owner decisions (2026-10-02)

1. **Concurrency 3** for the delegate pool.
2. **`full` is allowed by default, including from chat.** The rule that a
   child's profile may not exceed its parent's still holds, and `intent.md`
   never-rules still outrank everything. (Replaces the proposed
   chat-gets-read-only default; §2's "chat-minted delegations default to
   `read_only`" no longer applies.)
3. **Record the child's pid** so cancel really kills a running child. Cancel
   signals the child's process group; the job row becomes `canceled`.
4. **Move long codex work off the main lane.** Refinement: `plan.run` and
   `task.run_codex` get their own **single-worker "long" lane** (keeps their
   current one-at-a-time ordering and thread-safety assumptions) rather than
   joining the 3-wide delegate pool. Three lanes total: main, long (1),
   delegate (3).
5. **Record every child's brief** and structured result, successful or not,
   for the Phase 4 learning loop. They live on the job row (`payload` /
   `result`) and in the run ledger, so no new table.

## As built (steps 1-3)

- `CodexClient.complete(sandbox_mode=, timeout_seconds=, on_start=)`: per-call
  sandbox and wall limit beating config; `on_start(pid)` for cancel.
- `lisan/tools/delegation.py`: `delegate()` (validate + enqueue, `max_attempts=1`),
  `run_delegation()` (intent re-check, isolated prompt with no memory, ledger
  row `origin="delegate"`), `list/show/cancel_delegation`,
  `reap_overdue_delegations()`. CLI: `lisan delegate run|list|show|cancel`.
- Queue: `claim_next_job(exclude_job_types=, type_limits=)` (the cap is counted
  inside the claim transaction, so it holds across threads and processes);
  `jobs.child_pid`; `cancel_job` kills a running child's process group; the
  worker never overwrites a cancelled job with "failed".
- Scheduler: three lanes (main / long x1 / delegate xN) in `run_scheduler_loop`
  (`lanes=False` restores the old single lane). Lane-aware sleep calculation.
- Escalation: `agent.delegate` failures are reported with the real cause and
  are never auto-retried (the generic "one second chance" is skipped).
- Not in the work order, found while building: an orphaned child (scheduler
  process died) would have been requeued by the 45-minute stale reclaim and
  run twice. The reaper fails it instead, and kills the orphan only if the
  recorded pid still looks like codex (pids get reused).

Verified with the real `codex` binary against a scratch vault: a read-only child
read a file and answered correctly; asked to write, it was refused by the
sandbox and said so.

Known gaps: `task.run_codex` still gets the generic second chance after a
timeout (pre-existing; same ambiguity argument applies). Cancel for a plan's
running children arrives with step 4.

## Trial results (2026-10-02, run by Claude on a scratch queue, real `codex`, real scheduler)

Subject: a draft runbook of this very layer (`~/Desktop/lisan-delegation-trial/`)
with 7 deliberately planted errors (answer key kept outside the trial folder).

| Probe | Result |
|---|---|
| 3 read-only auditors, structured output, in parallel | all started in the same second; 44s/57s/63s each, ~63s wall vs ~164s sequential (2.6x). **7/7 planted errors found**, each with the right fix and file:line; 0 false positives; 2 further findings that were *true* (my key was imprecise): an `archived` job status outside `JOB_STATUSES`, and failed Adjutant schedules never block |
| 1 `workspace_write` editor applying the corrections | all 9 fixes applied, correct statements untouched, self-reported changed lines matched the real diff exactly, control file byte-identical. Style: some fixes paste the auditor's correction verbatim instead of reading as runbook prose |
| `workspace_write` child told to write outside its workspace | refused by the sandbox, said so honestly, file absent |
| 4 children against 3 slots | exactly 3 ran, 1 queued, then ran |
| 25s timeout on a `sleep 200` child | failed with the clear message, process group gone, **no retry**, no second-chance job |
| cancel a running child | recorded pid alive; after cancel the `sleep 300` grandchild was gone; job stayed `canceled` |

Defects the trial found, fixed in the same commit: the ledger recorded a cancelled
child as `failed` ("exit code -9") instead of `canceled`; delegate self-episodes
embedded the whole multi-line brief and read "a agent.delegate job"; the list view
dumped structured results as raw JSON.

Open observations (not fixed): a cancelled child produces no self-episode (the
episode query only covers succeeded/failed); the scheduler process leaves
`child_pid` to be cleared by the worker thread, so a SIGKILLed scheduler relies on
the reaper (unit-tested, not exercised live); no token/cost accounting exists, so
the only budgets are counts and wall time.

## Steps 4-5: design as built (refines §4-§5 above)

**The join is a table, not payload state.** `delegation_groups(group_id, kind,
parent_ref, child_job_ids, continuation_type, continuation_payload, state)`.
`settle_groups()` finds waiting groups whose children are all terminal
(succeeded / failed / canceled / archived / gone), **enqueues the continuation
job under a deterministic id** (`job.join.<group_id>`, a duplicate is a no-op),
then marks the group `settled`. Enqueue-then-mark means a crash between the two
is repaired by the next sweep. It runs (a) after every child finishes (fast
path) and (b) at the start of every worker drain (the guarantee: it also covers
a child cancelled from the CLI, or reaped after a crash).

**Idempotent launch.** Children of a fan-out get deterministic job ids
(`job.child.<group_id>.<i>`) and `enqueue_job(job_id=)` ignores a duplicate, so
a re-run of a half-launched fan-out cannot double-launch.

**Plan `fanout` step.** `{kind: "fanout", description, children: [{brief,
profile?, timeout_seconds?, working_directory?, result_schema?}], join: "all" |
"best_effort"}`. At most `delegation.max_children` (6). Validated at plan
creation (profile <= the plan's ceiling, timeout bounds, brief present), so a
bad fan-out fails fast, not at 3am. The step goes `pending -> waiting ->
done|failed`; the plan counts as *active* while waiting. `join: all` fails the
step if any child failed; `best_effort` fails it only if every child failed and
otherwise continues with the failures listed in the step result. Children are
never auto-retried; `lisan plan resume` re-runs a failed fan-out as a fresh
group. `cancel_plan` on a waiting plan cancels its children (killing running
ones) and ends the plan as `canceled`.

**Chat `delegate` tool.** Asynchronous only: it returns a handle at once. All
children of one call share a group; when the group settles, one
`agent.delegate_report` job (long lane) writes **one** capture turn for the whole
group and sends the owner **one** message with each child's outcome. Refused
from inside a plan (`_inside_a_plan`), for the same amplification reason as
`create_plan` and `schedule_task`.

## Steps 4-5 as built, and what the real runs found (2026-10-02)

Built exactly as designed above, plus: `lisan plan add --steps-file steps.json`
(the way to write a fanout step), `cancel_plan` / `resume_plan` for waiting
plans, and the owner's completion message for any plan now ends with the last
step's result (before, a plan ending in a codex step said "completed" and a
checklist, never the answer).

Real runs (real `codex`, real lanes, scratch queue, owner messages captured to a
file with `LISAN_NO_OUTBOUND` on):

- A plan with a 3-way fanout (workers counted test functions in three real test
  files) plus a final step that added them, and a chat `delegate` call with 2
  more workers: 5 children against 3 slots, never more than 3 running, 46s
  total. Every count matched `grep` (31, 14, 4 -> 49; 10, 15). One message per
  group/plan, one capture turn each, both visible in the scratch vault's
  transcript; both groups `settled`; ledger rows `delegate x5, plan x2`.
- Cancelling a plan while its workers were running: children `canceled`, plan
  inactive, no continuation fired.

**Defect found by that second run, fixed:** `codex exec` runs every command in
its OWN process group, so killing the child's process group (timeout, cancel,
reaper) left the real work running — a build, an ssh session. The step-2 cancel
trial passed by luck. Worse, a timed-out call blocked until the orphan finished,
because the orphan held the output pipe open. Fix: `lisan/tools/proctree.py`
kills the whole descendant tree found by parent pid (never by name: an unrelated
Codex desktop daemon runs on this machine), freezing the root first so it cannot
spawn mid-walk, and refusing init / ourselves / our ancestors. The test fakes
now reproduce codex's real shape (a command in a new session); the old
group-only kill fails 3 of them.

Also fixed on the way: the cancel branches in the worker `continue`d past the
`max_jobs` check; a plan's failure left later steps `pending` on the job row
while the report said `skipped`; `delegate()` validation is now shared
(`normalize_child_spec`) so plan creation rejects a bad fanout up front.

Not done: no token/cost accounting; `task.run_codex` still gets the generic
second chance after a timeout; a cancelled child writes no self-episode.
