# WO-ADJUTANT: Adjutant Execution Layer + Commander's Intent

Status: CODE COMPLETE 2026-07-24 (steps 1–7 shipped; the calibration
soak below is the remaining gate before `enabled: true`)
Binding spec for the execution layer. Amended from the original draft
where the repo's reality or an implementation ruling superseded it; every
amendment is marked **[RESOLVED]** with the ruling.

---

## 0. Context and design philosophy

Lisan is a local-first, deterministic-first memory vault (Listener ->
Writer -> Skeptic -> Interlocutor capture pipeline; Dreamer maintenance;
SQLite + FTS + embedding retrieval; markdown records with JSON
frontmatter). This work order adds an **execution layer** modeled on the
continental staff system, without modifying Lisan's core memory
guarantees.

Three components:

1. **`intent.md`** — a primer file: commander's intent. Goals,
   constraints, standing delegations. The authority document every
   execution decision is checked against.
2. **The Adjutant** — modules that poll the vault for actionable
   records, check them against intent, execute via the existing provider
   abstraction (Codex default), and report results back **through the
   existing capture pipeline**.
3. **Ingestion adapters** — optional capture sources feeding external
   signals into capture.

### Non-negotiable design principles

- **Lisan's memory core is not modified.** No changes to
  Listener/Writer/Skeptic/Interlocutor logic, retrieval scoring, or
  compartment enforcement, except the additive schema fields in §3.
- **Deterministic-first.** Polling, gating, scheduling, and logging are
  pure Python/SQL. LLM calls happen only inside task execution and only
  when a task requires generation or judgment.
- **Execution results re-enter through the front door.** The Adjutant
  reports by calling the capture pipeline; results are triaged, drafted,
  and Skeptic-reviewed like any other input. The executor never writes
  episodes/knowledge/state directly.
- **Default deny.** Every scope starts report-only. Authority is widened
  per-scope, per-capability, explicitly, in intent.md.
- **Everything is auditable.** Every Adjutant decision is logged with
  the intent rule and intent version that produced it.

### [RESOLVED] Settled forks (2026-07-23, owner-ratified)

The draft was written against a partially imagined codebase ("Hermes").
Rulings against the real repo:

- **Config is `config.json`**, not config.yaml. Adjutant/ingest settings
  are JSON sections there and in `config.example.json`.
- **Module layout is flat**: `lisan/tools/adjutant_*.py` (and
  `lisan/tools/intent.py`), matching the repo convention — not a
  `lisan/adjutant/` package.
- **Telegram reuses the existing bot** (`lisan/tools/telegram_bot.py`).
  No parallel `lisan/ingest/telegram.py` long-poller. `notify` and
  confirmation pings ride the existing delivery path; the bot's inbound
  parsing gains confirmation-id handling.
- **Schedules are hybrid**: `schedule` markdown records are the
  owner-editable definition; on index they materialize into the existing
  jobs table, and the existing scheduler machinery drives firing. One
  scheduling brain.
- **Reporting uses `capture_text()`** (the real front door), tagging
  source via the same conventions the Telegram bot uses; artifacts are
  referenced by path in the result body.

---

## 1. Commander's intent: `primer/intent.md` — SHIPPED 2026-07-23

Implemented in `lisan/tools/intent.py`; contract pinned by
`tests/test_intent.py` (32 tests).

- Lives at `<vault>/primer/intent.md`, sibling to `identity.md`. Seeded
  by `lisan init`; `lisan intent init` for existing vaults.
- JSON frontmatter: `id: intent-current`, `type: intent`, `created`,
  `updated`, `status`, `version` (positive int), `review_after`.
- Required body sections, enforced by the validator: `# Mission`,
  `# Priorities`, `# Standing Delegations`, `# Escalation Rules`,
  `# Never`.
- **Versioned:** every CLI edit snapshots the prior version to
  `primer/intent-history/intent-<UTC-stamp>.md` and bumps `version`.
  Out-of-band edits are detected by content hash (sidecar
  `intent-history/.known-hash`) at Adjutant startup and snapshotted then.
- `lisan validate` validates intent.md when present. An invalid
  intent.md causes the Adjutant to refuse to start (fail closed).
- **[RESOLVED] Invalid edits are kept, never reverted.** The owner's
  words outrank the validator; `lisan intent edit` warns loudly (exit 1)
  and execution halts until fixed. See §2.7 for the loudness
  requirement on the halt itself.

### 1.3 Standing Delegations

Fenced JSON block inside `# Standing Delegations`. Keyed by **scope** —
see the 2026-07-29 ruling below, which supersedes the word "arena"
throughout this section:

```json
{
  "defaults": { "mode": "report_only" },
  "scopes": {
    "lisan-dev": {
      "mode": "execute",
      "capabilities": ["run_local_scripts", "read_files", "write_files", "web_research"],
      "confirm_required": ["git_push", "publish"]
    },
    "finance": {
      "mode": "report_only",
      "capabilities": ["read_files", "web_research"],
      "confirm_required": ["*"]
    },
    "legal": {
      "mode": "report_only",
      "capabilities": ["read_files"],
      "confirm_required": ["*"],
      "outbound_comms": "never"
    }
  },
  "global": {
    "spend_money": "confirm_always",
    "send_outbound_message": "confirm_always",
    "delete_files": "confirm_always",
    "max_task_wall_seconds": 600,
    "max_tasks_per_cycle": 5
  }
}
```

Capabilities (enum, extensible): `read_files`, `write_files`,
`run_local_scripts`, `web_research`, `send_outbound_message`,
`spend_money`, `git_push`, `publish`, `delete_files`.
Modes: `report_only`, `execute`, `disabled`.

### [RESOLVED] Resolution contract (supersedes the draft's ambiguity)

Order: never-rules -> global rules -> scope rules -> defaults.
Restrictiveness: EXECUTE < CONFIRM < REPORT_ONLY < DENY; **most
restrictive wins on conflict.** Specifically:

- A capability **named explicitly** in a scope's `confirm_required` is
  a **grant-with-confirmation** — it need not also appear in
  `capabilities` (this is what the lisan-dev example above means:
  git_push is permitted, behind a human gate).
- `"*"` in `confirm_required` only **tightens** the granted
  `capabilities` list; it never widens it. finance's `"*"` grants
  nothing by itself — default deny holds.
- **Never-rules outrank confirm_required grants.** A capability both
  named in confirm_required and covered by a never-rule (a scope's
  `outbound_comms: never`, or a global `never`) resolves DENY, not
  CONFIRM. Pinned by `test_never_rules_beat_confirm_required_grants`.
- `report_only` mode outranks a global `confirm_always`: the task
  reports, it does not queue for confirmation.
- An unlisted scope falls to `defaults.mode`; a default of `execute`
  still grants no capabilities (default deny), `disabled` denies. An
  *absent* scope resolves `no_scope` — same restrictiveness, different
  rule, because the owner's remedy differs (tag the record vs add a rule).

### [RESOLVED 2026-07-29] The delegation axis is `scope`, not `arena`

**This supersedes every use of "arena" above in a delegation sense.** The
JSON example in §1.3 and the resolution contract now read `scopes` and
"scope"; `arenas` remains accepted forever so an adopted document keeps
resolving.

The defect. Delegation was grafted onto the life-dimension enum
(`domain_primary`, historically `arena`). A real adopted `intent.md`
declared eight areas of responsibility — projects, a household, a
workplace, family-legal matters — while records carried life dimensions
(`cross_arena` on the large majority, `relational`, `work`). The overlap
was **empty**. (The scope names themselves are personal and stay out of
this repo; `lisan intent scopes` prints them locally.) Every task from every
source resolved through `defaults`, and because defaults grant no
capability list, EXECUTE was unreachable by construction. Measured on the
live vault 2026-07-29: 8 declared scopes, 0 reachable; 245 of 248 cycles
logged `tasks=0`; 3 verdicts in six days, all `report_only`, all from one
record.

Why it stayed invisible for a month — and this is the transferable
lesson. `files.arena` was populated as `COALESCE(arena, domain_primary,
arena_primary)`. That fallback is *correct* for the life-dimension axis
(same axis, older name) and was catastrophic for the gate reading it as
delegation: an absent authority declaration silently became a plausible
life dimension, so the gate always had something to resolve and never
reported a gap. **A default that substitutes for a missing declaration
converts a loud failure into a silent one.** Same family as
`unreachable_policy: skip` leaving the semantic lane dead while embed
jobs reported success.

The ruling:

1. **Two axes, permanently distinct.** `domain_primary` is a life
   dimension from a fixed enum, for retrieval and the self-model.
   `scope` is owner-defined free text and is the only axis intent.md is
   resolved against. `cross_arena` stays a domain enum value; renaming it
   is a schema migration and is out of scope here.
2. **`scope` never falls back.** `lisan/tools/scope.py::record_scope`
   reads one field. Absent means absent, and the gate says
   `no_scope` rather than inventing a match. Pinned by
   `test_record_scope_never_falls_back_to_domain` and
   `test_index_keeps_scope_null_while_arena_still_mirrors_domain`.
3. **Only the owner assigns scope.** The capture pipeline never sets one;
   a writer-proposed `scope` is dropped and the drop is logged
   (`fanout.open_loop.scope_ignored`). A model inferring areas of
   responsibility is how the original mismatch was manufactured. Both v2
   writer prompts now say so explicitly.
4. **Therefore captured work is reportable, never unattended-executable.**
   This resolves the "why does the writer never tasks anything" question
   the other way round than expected: the writer's restraint is correct,
   and conversational capture was never the right source of *authority*.
   Authority attaches to work the owner created deliberately —
   `lisan new loop --scope <name>`, a schedule record, an approved
   confirmation. The definition-of-done test now pins the full arc:
   captured tasking -> `no_scope` report -> owner assigns scope ->
   execute.
5. **Confirmations carry their own scope.** Previously the scope was
   smuggled through `domain_primary` and read back via the `arena`
   coalesce. Now `new_confirmation(scope=...)` writes the real field, so
   the gate's re-check at execution time resolves the same rule the owner
   approved under. Without this an approval silently degrades to
   `no_scope`.
6. **Casing is normalized at every boundary** (stripped, lowercased).
   Observed drift included `"Lisan System"` beside `system`; a delegation
   that misses on a capital letter reads as a considered rule and behaves
   as no rule. The validator flags non-canonical `scope` values.
7. **`lisan intent scopes` is the instrument.** Declared scopes beside
   scopes present on pollable records, with the gap named in both
   directions. Run it after editing Standing Delegations. This is the
   command that would have caught the whole defect on day one.

Calibration consequence: the soak that ran 2026-07-23 -> 07-29 measured a
gate with one reachable verdict. **It is not evidence of anything and
extending it buys nothing.** A fresh soak starts when at least one scope
is reachable — verify with `lisan intent scopes` before starting the
clock. EXECUTE first becomes reachable at that moment, which is when the
`enabled: true` audit genuinely begins.

Migration for existing installs: additive `scope` column on `files` and
`adjutant_log`, applied by `ensure_index_schema` on the next index write
(zero-migration guarantee holds). Old `adjutant_log` rows keep their
`arena` values — accurate history of what those verdicts were computed
from; readers `COALESCE(scope, arena)`.

### 1.4 CLI (shipped)

```
lisan intent show | init | edit | history | scopes | check <scope> <capability>
```

`check` runs the real resolver against the live intent — the same
function the gate (§2.3) wraps.

---

## 2. The Adjutant

### 2.1 Module layout [amended per settled forks]

```
lisan/tools/
  intent.py                  # shipped (step 1)
  adjutant_poller.py         # finds actionable records (pure SQL)
  adjutant_gate.py           # (task, scope, capabilities) -> verdict + audit log
  adjutant_executor.py       # runs tasks via providers; sandboxing; timeouts
  adjutant_reporter.py       # formats results, submits via capture_text
  adjutant_confirmations.py  # pending-confirmation queue
  adjutant_runner.py         # the cycle loop / daemon entry
lisan/schemas/
  adjutant_task_v1.json
  adjutant_result_v1.json
prompts/
  adjutant_research_v1.md
  adjutant_plan_step_v1.md
```

### 2.2 What is "actionable"?

The poller selects, via SQL against the existing index:

1. **Open loops** with `status: active`, a due date now-or-past or
   `execute_asap: true`, whose `scope` is not `disabled`.
2. **Decision records** with any `execution_steps` step
   `status: pending`.
3. **Scheduled tasks**: `schedule` records whose materialized job is due
   (hybrid model, §3).
4. **Confirmed tasks**: approved items in the confirmation queue.

Ordering: confirmations/escalations first, then (priority match against
intent Priorities, due date, created date). Cap: `max_tasks_per_cycle`.

**[RESOLVED — priority vocabulary, 2026-07-23]** Priority match is
deterministic token overlap between the task **summary only** and the
intent Priorities lines (first matching line wins; unmatched tasks rank
after all matched ones, then by due date). Arena names are excluded from
matching — they are everyday words ("work", "financial") and
false-match priority prose. The owner steers ranking by writing
priorities in the tasks' own vocabulary; no model opinion enters the
ordering. A task with an approved confirmation rides the confirmation
lane only (never double-selected as a pending loop).

### 2.3 The gate

Pure function, no LLM. Wraps `intent.resolve_capabilities` with the
deterministic task_kind -> required-capabilities mapping (e.g.
`research -> [web_research, write_files]`; `run_script ->
[run_local_scripts, read_files, write_files]`). Every verdict is written
to `adjutant_log`: timestamp, task id, scope, capabilities, verdict,
matched rule, intent version. A task whose compartment/blocked_contexts
would prevent retrieval of its own scope context is DENIED and flagged
(misfiled task).

### 2.4 Execution

- Existing provider abstraction; default `codex`, honoring `CODEX_BIN`.
- Task kinds (v1): `run_script` (allowlisted script dirs only, args from
  the task record, never from generation), `research`, `collect`
  (allowlisted paths -> evidence-candidate turns via capture), `draft`
  (Writer-style schema-backed generation into `drafts/`), `notify`
  (always behind `send_outbound_message` gating).
- Sandboxing v1: subprocess, per-task scratch cwd, wall-clock timeout
  from intent, stdout/stderr captured in full, no network for
  `run_script` unless the script dir is marked `network_ok`.
- Idempotency: `task_runs` rows per attempt; two failures ->
  `task_status: blocked`, surfaced in batch review. No infinite retries.

### 2.5 Reporting — the closed loop

On completion the Adjutant composes a structured result
(`adjutant_result_v1.json`) and submits it via
`capture_text(conversation_id="adjutant", ...)`. The pipeline does what
it always does; the Skeptic flags overclaiming; the originating
open_loop resolves **only after** the resulting draft is promoted. **The
executor cannot write memory directly.**

### 2.6 Confirmations and escalation

- CONFIRM verdicts write a `confirmation` record: task summary, exactly
  what will happen, cost/risk, expiry (default 7 days).
- Surfacing: batch review section; `lisan confirm list | approve <id> |
  deny <id>`; Telegram notify when configured.
- Expired -> `task_status: expired`, appears in batch review.
- Approval/denial is itself captured and becomes a decision record.
- Outbound confirmations render the **full outgoing content** — the
  human approves the actual message, not a summary.

### 2.7 Runner and daemon

```
lisan adjutant run | daemon | status | log [N]
```

Daemon interval from config (default 15m); lockfile in the vault root —
two daemons on one vault is an error. launchd plist example in docs/.

**[RESOLVED — loudness requirement]** A halt is never silent. When the
daemon refuses to start or stops (invalid intent.md, lock conflict,
anything), the reason and timestamp must land where the owner will see
it: `lisan adjutant status` at minimum, and once `notify` exists
(step 5), an owner ping — "Adjutant halted: intent.md invalid since
<time>". A safety mechanism nobody notices firing is a system that
quietly stopped working. (Same policy as the failure-escalation rule
that governs the job worker.)

---

## 3. Schema additions (all additive; zero migration)

New record types: `intent` (shipped, primer-located), `schedule`
(hybrid: markdown definition materialized into the existing jobs table),
`confirmation`.

New optional frontmatter fields (allowed, never required):
- `open_loop`: `execute_asap`, `task_kind`, `task_payload`,
  `task_status: pending|running|blocked|expired|resolved`.
- `decision`: `execution_steps: [{step, task_kind, task_payload, status}]`.

Writer prompt updates, versioned v2 alongside v1, selected by config
flag `adjutant.enabled`: populate task fields only for genuinely
actionable instructions; aspirational statements stay task-free.

New SQLite tables: `adjutant_log`, `task_runs`, `confirmations` — all in
`rebuild-index`. In `adjutant_log`, **`task_id = 'cycle'` is reserved**
for cycle-level events (verdict column carries the event name: `halt`,
`cycle`, `intent_oob_edit`) so `adjutant status` reads one log for both
task verdicts and lifecycle; no record may use that id.

**[RESOLVED — table lifecycle ruling, 2026-07-23]** Definition is
memory, runtime is index, applied per-table: `adjutant_log` and
`task_runs` are pure runtime history (peers of `retrieval_log`) and
**survive** rebuild. `confirmations` **mirrors markdown records**, so it
is derived state: wiped and repopulated from records on every rebuild —
a mirror that survived reindex could let a hand-edited or
backup-restored confirmation drift from its row, in a table that gates
real-world actions. Step 4's confirmation manager keeps the mirror
synced incrementally between rebuilds; any state that turns out to be
genuinely runtime-only (e.g. "notified owner at <time>") belongs in a
runtime table or adjutant_log, never in the mirror.

## 4. Ingestion adapters (v1: minimal)

`lisan/tools/` gains fswatch polling (configured dirs -> capture as
evidence-candidate turns) and stubs for email/SMS. Telegram: existing
bot (see settled forks). Every ingest turn is tagged with its source in
the transcript.

Config (`config.json`):

```json
{
  "adjutant": {
    "enabled": false,
    "interval_minutes": 15,
    "script_dirs": [],
    "collect_paths": []
  },
  "ingest": {
    "fswatch_paths": [],
    "telegram_token_env": "LISAN_TELEGRAM_TOKEN"
  }
}
```

`adjutant.enabled: false` keeps `adjutant run` in --dry-run.

## 5. Security and privacy requirements

- Compartment enforcement applies to Adjutant context assembly exactly
  as to chat retrieval; test the blocked-context leakage case explicitly.
- No secrets in the vault: tokens from env vars only; validator warns on
  credential-pattern matches.
- The maximally-gated template entry ships as an obvious placeholder
  (see §1.3 and the 2026-07-29 ruling): a copyable `legal` example was
  itself part of the defect.
- `lisan backup create` must include the new tables and
  `intent-history/`.

## 6. Implementation order — ALL SHIPPED

1. intent.md: template, parser, validator, CLI, versioning — 2026-07-23,
   065fe2d (+ teaching template & sentinel gate, 22a18df).
2. Schema additions, zero migration proven — 3b7be77 (+ mirror ruling,
   239b436).
3. Gate + poller + dry-run cycle — edd58a9.
4. Executor local kinds, reporter via capture, confirmations — 39a7438.
5. research + notify via the existing Telegram bot — d77a418.
6. Schedules materialized through jobs, daemon + lock, fswatch,
   launchd doc — 29fb5be.
7. Writer v2 behind the flag; definition-of-done integration test
   green — c39296f.

**Remaining gate — the calibration soak (owner-run, not code):** set
`adjutant.calibration: true` and run the daemon for a period, watching
adjutant_log verdicts and which turns acquire task fields.

**[RESOLVED — calibration posture, 2026-07-24]** The original
prescription ("enabled: false with v2 writers on") was unconfigurable:
v2 writer selection was gated on `enabled`, and flipping `enabled`
would not have been dry. `adjutant.calibration` is the named posture:
v2 writers ON, every cycle FORCED dry regardless of `enabled` —
calibration outranks enabled unconditionally, pinned by test. There is
no combination of config values that both mints task fields and
executes, except the owner's explicit key-turn: calibration off,
enabled on. (Calibration also soaks on an unadopted sentinel intent —
dry cycles act on nothing; plain enabled still halts on sentinels.)

The asymmetry to audit: false taskings (aspirations that got a
task_kind) are the failure that matters; missed taskings cost one
command. The audit trail from real days, not the test suite, is what
earns the key-turn — and even then, execution starts at the scopes the
owner has explicitly granted.

## 7. Testing requirements

- Unit: gate resolution matrix (done for the resolver; extend to the
  task-kind mapping), poller SQL against a fixture vault, confirmation
  expiry, retry/blocking.
- Integration (definition of done): fake provider — instruction turn ->
  open_loop with task fields -> dry-run verdict -> execute (echo script)
  -> result re-captured -> Skeptic review -> originating loop resolved.
- Negative: disabled scope never selected; unscoped record reports;
  blocked-context leakage
  denied and logged; script outside allowlist refuses; invalid intent.md
  prevents daemon start.

## Future-work notes

- **Deviation-drive candidate pattern #2 (2026-07-24):** "ambient
  sqlite_path reaches further than you think." The global default DB
  path bit twice in two days: a test's broken monkeypatch leaked two
  adjutant.cycle jobs into the LIVE jobs table (one fired against the
  old worker, failed 3x, and paged the owner through the escalation
  ladder), and a hermetic-looking test resolved a check-in subject
  through the developer's real alias table. Anything that writes
  through a default db_path should be suspicious in tests and explicit
  in tools; a future gate test could refuse enqueue_job calls whose
  db_path was defaulted inside pytest.
- **Deviation-drive candidate pattern (2026-07-23):** "helper timeouts
  that are actually held locks." An uncommitted INSERT on the cycle's
  connection stalled every helper that opened its own SQLite connection
  into silent five-second lock waits — a degradation that never fails a
  test and always ruins a daemon. Fixed by committing after each verdict
  log; the *pattern* (quiet stalls whose real cause is a lock held
  upstream) belongs in the deviation drive's vocabulary.
- **Double-expiry escalation (ratified 2026-07-23):** one expired
  confirmation is bookkeeping; the same task expiring twice is the owner
  avoiding or not seeing a decision. Batch review flags it
  (REPEATEDLY EXPIRED) as of step 4; once notify exists (step 5), a
  second expiry should also ping the owner once.
- **Template sentinel gate (ratified 2026-07-23):** the seed intent
  template ships with 1970-01-01 dates; while any of created/updated/
  review_after carries the sentinel, enabled cycles halt loudly
  (uncustomized authority is no authority). Dry-run proceeds — it acts
  on nothing.

## 8. Out of scope (v1)

- Email/SMS live adapters (stubs only).
- Multi-step autonomous planning (plan synthesis is a future Dreamer
  task, specced separately).
- Any UI beyond CLI + Telegram.
- Spending money without confirmation — permanently out of scope, not
  just v1.
