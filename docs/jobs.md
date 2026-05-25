# Jobs

Lisan now uses a small SQLite-backed job queue for work that is safe to defer out of the immediate chat path.

## Why it exists

- Keep chat responses fast.
- Keep memory writes durable without depending on a separate queue service.
- Make background work inspectable from the CLI.
- Preserve auditability when slower work fails.

## Synchronous vs Background

Synchronous work still happens immediately when the user needs a visible result:

- transcript append
- Listener / Writer / Skeptic / Interlocutor work required for the current turn
- explicit memory corrections and direct record creation

Background work is queued when it can safely lag behind the user response:

- SQLite index rebuilds
- Analyst pattern scans
- Dreamer maintenance passes
- optional Skeptic pattern reviews

## Queue Semantics

Jobs are stored in the same SQLite database as the rest of the local index.

Supported statuses:

- `queued`
- `running`
- `succeeded`
- `failed`
- `canceled`
- `retry_wait`

Priority is numeric. Lower numbers run first.

Retry behavior is durable and visible:

- a failure moves the job to `retry_wait` until it is retried or manually requeued
- repeated failures stop at `max_attempts`
- failures are not hidden

## Priorities

Lower numbers run first.

- `writer.extract_turn`: `20`
- `skeptic.review_pattern`: `30`
- `index.rebuild_record`: `40`
- `ingest.artifact.extract`: `50`
- `pattern.audit`: `60`
- `analyst.scan`: `70`
- `dreamer.maintenance`: `80`
- `manifest.regenerate`: `90`

## Coalescing

Some jobs are intentionally merged so repeated chat turns do not create an unbounded backlog.

Aggressively coalesced:

- `analyst.scan`
- `dreamer.maintenance`
- `pattern.audit`
- `manifest.regenerate`
- `index.rebuild_all`

Coalesced by record id:

- `index.rebuild_record`
- `skeptic.review_pattern`

Not coalesced by default:

- `writer.extract_turn`
- `ingest.file.parse`
- `ingest.artifact.extract`

If an equivalent queued job already exists, Lisan updates that job instead of creating a duplicate. If a matching job is already running, Lisan allows one queued follow-up, but not an unlimited chain of duplicates.

## Running a Worker

Run queued jobs with:

```bash
python3 -m lisan jobs run
```

Useful variants:

```bash
python3 -m lisan jobs list
python3 -m lisan jobs show <job_id>
python3 -m lisan jobs retry <job_id>
python3 -m lisan jobs cancel <job_id>
python3 -m lisan jobs audit
```

The worker accepts `--vault`, `--db-path`, `--provider`, `--model`, `--worker-id`, and `--max-jobs` when you need to point it at a non-default workspace or run a bounded batch.

## What the Worker Does

Current job handlers are thin wrappers around existing code paths:

- `index.rebuild_record` calls the rebuild-index path
- `analyst.scan` runs the longitudinal Analyst pass
- `dreamer.maintenance` runs a Dreamer maintenance task
- `skeptic.review_pattern` reviews a pattern record
- `writer.extract_turn` replays a turn through the existing capture path

## Stuck Jobs

A running job is considered stuck if it has exceeded the queue timeout.

- `python3 -m lisan jobs audit` lists stuck jobs separately from ordinary long-running jobs
- `python3 -m lisan jobs reap-stuck` moves stale running jobs back to `retry_wait` by default
- add `--fail` to mark them failed instead of retrying them

## Inspecting Failures

Use `python3 -m lisan jobs audit` to see:

- queued jobs by type
- failed jobs
- retry-wait jobs
- long-running jobs
- last successful Analyst and Dreamer runs
- records waiting for index rebuild

If a job fails, the error text stays in the queue row and shows up in the audit report.

## Why Analyst and Dreamer Are Background Jobs

Analyst and Dreamer are the slowest and most speculative parts of the memory system.

They are background jobs because:

- they scan across many records
- they can be retried without changing the immediate response
- they should never block the user's chat turn
- they are easier to inspect and govern when separated from turn handling

The default policy is to treat their output as deferred maintenance, not as part of the immediate conversational reply.

## Turn Scheduling

Lisan does not queue Analyst or Dreamer on every trivial turn.

The default turn policy is conservative:

- short or skipped turns do not queue background work
- Analyst usually waits for enough changed records, an explicit self-analysis request, or a high-salience change
- Dreamer usually waits for reviewed patterns, stale claims, or a sufficiently old last Dreamer run

This keeps the queue small and predictable during normal chat use.
