# Work Order — Response Quality (WO-QUALITY)

**Status: SPECIFIED, NOT SCHEDULED.** Five concrete improvements derived
from a transcript quality audit of the 2026-08-19 session. Each item is
independently shippable; ordered by impact.

**One-line goal:** close the gaps between how the agent responds and how
a frontier model would respond in the same situation — passivity,
confabulated limitations, false status reports, unnecessary friction, and
silent failures.

**Source:** a manual review of `vault/transcripts/2026-08-19.md` that
identified exchanges where the agent's behavior diverged from
frontier-model expectations, then traced each finding to its root cause
in the codebase. Findings that were external (LLM generation artifacts,
infrastructure outages) or already addressed (existing prompt rules the
LLM didn't follow) were excluded. The full analysis is archived at
`~/Desktop/lisan-quality-recommendations.html`.

**Governing pattern:** the core issue across the transcript is
*passivity* — the agent reports what it doesn't know instead of
investigating. A frontier model says "I don't know offhand, let me
check"; this agent says "I don't know" and waits for the owner to say
"then go look." Every item below either reduces passivity directly or
closes an instrument gap that makes passivity the only honest option.

---

## 1. Proactive investigation rule (prompt)

**Problem:** When the owner asks about internals (scheduling, config,
code behavior) and the answer isn't in GROUND_TRUTH or CAPABILITIES,
the agent reports the gap and waits. Three rounds on self-audit cadence
(lines 163–178); four rounds on the backend model (lines 83–104) — each
time the agent had the tools to answer but didn't use them until
explicitly told to.

**Root cause:** The conversation prompt (`prompts/conversation_v1.md`)
teaches honesty about gaps but not initiative to close them. The
GROUND_TRUTH section says to answer self-state questions from the
snapshot; there is no instruction to investigate when the snapshot
doesn't cover the question.

**Change:** Add a rule near the GROUND_TRUTH section: when the owner
asks about internals and the answer isn't in GROUND_TRUTH or
CAPABILITIES, use `read_file` on the relevant source file before
reporting "I don't know." The honest answer to "how often do self-audits
run?" is not "I can't tell" — it's "let me check the scheduling code."

**Files:** `prompts/conversation_v1.md`

**Risk:** Low — prompt addition only, no code changes.

---

## 2. Retry on empty LLM response (code)

**Problem:** When the LLM returns empty, the agent tells the user "Say
that again and I'll take another run at it." The user's message is
already in context — the agent should retry silently, not shift the work
to the human.

**Root cause:** The empty-response handler at
`lisan/tools/conversation.py:137-142` catches `not response` and
immediately returns a hardcoded apology string. No retry is attempted.
The `_call_agent` closure captures everything needed for a second call.

**Change:** When the response is empty, call `_call_agent()` one more
time before falling back. If the retry also returns empty, surface the
failure — but reword it: "I wasn't able to process that — can you try
again?" rather than implying the user needs to re-type. One retry,
bounded, no loop.

**Files:** `lisan/tools/conversation.py`

**Risk:** Low — bounded retry, same closure, no new dependencies.

---

## 3. Job output validation (code)

**Problem:** The dreamer was dead for 17 days while `self_state` reported
it as successful. The dreamer-specific fix (`provider_error_mode="raise"`)
is in place, but the broader vulnerability remains: `mark_job_succeeded()`
fires whenever `dispatch_job()` returns without raising, regardless of
output quality. A job returning `None`, an empty dict, or a result
missing required fields is marked succeeded.

**Root cause:** In `lisan/tools/jobs.py:~1470`, success is defined as
"didn't raise." There is no output validation between `dispatch_job()`
returning and `mark_job_succeeded()` being called.

**Change:** Add a per-job-type output expectation registry — each job
type declares what valid output looks like (e.g., dreamer expects a
non-empty compaction result; analyst expects the `analyst_output` schema
fields). A return value that is `None`, empty, or missing declared fields
is treated as a failure: "job returned but produced no valid output."
This catches the entire class of silent-success bugs, not just the
dreamer variant.

**Files:** `lisan/tools/jobs.py`

**Risk:** Medium — touches the job dispatch pipeline. Needs tests for
each job type's validation, and a clear fallback for job types that
don't declare expectations (treat as pass-through, don't break existing
behavior).

---

## 4. Self-state instrument gaps (code)

**Problem:** When asked "what does your config say" and "what model are
you using," the agent couldn't answer because the information isn't in
GROUND_TRUTH. It then searched wrong paths (`config.toml` instead of
`config.json`) because the config file path isn't in the capability
manifest either. The entire provider/model chain is invisible to
self-state.

**Root cause:** `build_capability_manifest()` in
`lisan/tools/self_model.py:129-133` emits paths for repo, vault,
database, and skills — but not the config file. `snapshot_self_state()`
at line 272 reports version, commit, jobs, services, plans, and logs —
but not the active provider or model.

**Change:** Add `"config": str(config_path)` to the manifest's `paths`
dict. Add `"provider"` and `"model"` to the self-state snapshot, read
from `config.json` at snapshot time. This is durable across provider
changes because it reads whatever the config currently says, not a
hardcoded provider name. The deeper question of reading the provider's
own internal model config (e.g., Codex's `~/.codex/config.toml`) is a
separate, lower-priority item — the config.json routing info alone
answers 80% of the owner's question.

**Files:** `lisan/tools/self_model.py`

**Risk:** Low — additive fields, no existing behavior changes.

---

## 5. Capability boundary clarity for email (code)

**Problem:** The agent led the owner through a four-turn email
confirmation flow before discovering it couldn't send email. The
`NOT_BUILT` list includes "External communication" but the description
focuses on messaging "anyone other than the owner" — email to the owner
falls in an ambiguous gap. Meanwhile, Gmail auth exists in the skill
auth system (for reading), further muddying the picture.

**Root cause:** `lisan/tools/self_model.py:38-54` — the NOT_BUILT entry
"External communication" describes messaging other people, not email in
general. The conversation prompt at `prompts/conversation_v1.md:226-227`
says "no email, no texts" but this is buried in the ingestion-abilities
section, not the capability index the LLM reads every turn.

**Change:** Add an explicit NOT_BUILT entry: `{"name": "Email sending",
"detail": "Cannot send email to anyone, including the owner. Gmail auth
is for reading and searching only. Can draft text for the owner to
send."}` This surfaces in the capability index every turn. When email
sending is eventually built, remove the entry — the capability index
auto-updates.

**Files:** `lisan/tools/self_model.py`

**Risk:** Low — additive entry, no code behavior changes.

---

## Implementation decisions (to be recorded here when each item ships)

*(Empty — work not yet started.)*
