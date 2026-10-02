# Delegation work order

Status: **steps 1-3 built (2026-10-02); paused for a real-world trial before
steps 4-5 (plan `fanout`, chat `delegate` tool).**
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
