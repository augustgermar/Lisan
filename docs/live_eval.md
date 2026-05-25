# Live Behavioral Eval

`eval-live` is a development-only harness for exercising the real Lisan chat path against an isolated test vault.

## What it does

- Drives scripted conversations through the same turn handling code used by `lisan chat`
- Captures response text, timings, trace data, queued jobs, and file writes
- Compares observed behavior against structural expectations
- Writes markdown and JSON reports
- Records useful surprises and suggested follow-up changes
- Can wipe the isolated test run safely after completion
- When `--cycles` is used, each cycle gets its own isolated run directory, vault, state, trace set, transcript, and report

## Why this exists

Unit tests are useful for narrow behavior checks, but they do not show how the live app behaves across multiple turns, memory writes, retrieval, and job scheduling. This harness is for that gap.

## Safety model

- By default, each run uses an isolated vault under `.lisan_live_eval_runs/<run_id>/vault`
- It does not use the production vault unless you explicitly pass a dangerous override
- Wipe is marker-gated and only deletes a run that contains `.lisan_eval_vault`
- Before any scenario runs, the harness performs a provider preflight. If the provider or session directory is broken, the run is classified as `infrastructure_failed` instead of being scored as a normal behavioral failure.
- Vault/state isolation is separate from provider auth isolation. For Codex, the default is to keep the eval vault isolated while still using the shared authenticated Codex home.

## Commands

```bash
python3 -m lisan eval-live run
python3 -m lisan eval-live run --scenario basic_identity
python3 -m lisan eval-live run --cycles 3
python3 -m lisan eval-live run --cycles 3 --seed 1234
python3 -m lisan eval-live run --provider-auth shared
python3 -m lisan eval-live run --provider-auth isolated
python3 -m lisan eval-live run --provider-auth mock
python3 -m lisan eval-live run --wipe-after
python3 -m lisan eval-live run --cycles 5 --wipe-after
python3 -m lisan eval-live report <run_id>
python3 -m lisan eval-live list
python3 -m lisan eval-live wipe <run_id>
```

### Job options

- `--run-jobs-after-turn` runs queued jobs after each turn
- `--run-jobs-at-end` runs queued jobs once after the scenario
- `--no-jobs` disables worker execution
- `--seed` seeds cycle 1; later cycles use `seed + cycle_index`
- `--provider-auth shared` uses the user's normal authenticated Codex home while keeping the eval vault isolated
- `--provider-auth isolated` uses a per-run Codex home and requires separate auth/bootstrap
- `--provider-auth mock` uses a deterministic fake provider for harness-only runs

The default is to run jobs at the end of the scenario, not after every turn.

## How to read reports

Each cycle run creates:

- `.lisan_live_eval_runs/live_eval_<stamp>_cycle_###/transcript.md`
- `.lisan_live_eval_runs/live_eval_<stamp>_cycle_###/transcript.json`
- `.lisan_live_eval_runs/live_eval_<stamp>_cycle_###/reports/report.md`
- `.lisan_live_eval_runs/live_eval_<stamp>_cycle_###/reports/report.json`
- `.lisan_live_eval_runs/live_eval_<stamp>_cycle_###/traces/`
- `.lisan_live_eval_runs/live_eval_<stamp>_cycle_###/.lisan_eval_vault`

When `--cycles` is greater than 1, the harness also creates:

- `.lisan_live_eval_runs/aggregate_<stamp>/aggregate.md`
- `.lisan_live_eval_runs/aggregate_<stamp>/aggregate.json`
- `.lisan_live_eval_runs/aggregate_<stamp>/cycles.json`

If `--wipe-after` is enabled, the cycle directories can be deleted after their reports are written while the aggregate directory is kept for later inspection.

The report calls out:

- pass/fail status
- latency and LLM call summaries
- durable memory records created
- retrieval behavior
- unexpected negatives and positives
- proposed improvements

The aggregate report summarizes repeated failures, recurring surprises, slowest turns, LLM call counts, job failures, and cleanup status per cycle.

## Provider failures

If the local provider cannot create or use its session files, the eval may stop before any scenario is executed.

In that case:

- the report classification is `provider_failure` or `infrastructure_failed`
- behavioral expectations are not marked failed
- the report includes remediation commands

See [provider_diagnostics.md](provider_diagnostics.md) for the preflight checks and the common `~/.codex/sessions` permission fix.

If Codex auth exists only in your shared home, use `--provider-auth shared`. That keeps the eval memory isolated while letting the provider use the authenticated Codex session in your normal home directory.

## Interpreting surprises

- A negative surprise means the output or trace diverged from the structural expectation
- A positive surprise means the model behaved better than the baseline expectation in a useful way
- The oracle is intentionally structural, not wording-exact

## Why cycles are isolated

Cycles must not share memory state because a later cycle can otherwise inherit facts, jobs, or traces from an earlier one and make a bad behavior look fixed. The harness prevents that by putting every cycle in its own run directory with its own vault, sqlite state, logs, traces, transcript, and report.

This is especially important for:

- identity capture
- family-role perspective tracking
- memory extraction
- job scheduling
- safety refusal checks

## Using the output

The report includes proposed follow-up changes, but the eval loop does not apply them. Treat them as prompts for a manual Codex edit or a separate implementation pass.
