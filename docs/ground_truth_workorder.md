# Work Order — Ground Truth for Self-Referential Questions (WO-GROUND)

**Status: SPECIFIED, NOT SCHEDULED.** Written 2026-07-06, the last day of
frontier-model access, as the class-level fix behind a season of local
patches. No entry gate beyond a green suite; this can be the first thing
a post-Fable agent executes. Where this document conflicts with the code
as it then exists, reality wins — report the conflict.

**One-line goal:** the agent must never answer a question about ITSELF
(capabilities, status, auth, schedule, its own past actions) from memory
or plausibility when generated ground truth exists — and its memory of
itself must be stored in a way that cannot later impersonate ground truth.

## 1. The defect class, with its case history

Every incident below is the same defect: *the agent answers from vibes
when live truth is available.* Each got a correct local patch; the class
survived every one of them.

- Invented CLI commands (`lisan skills gmail auth`, `lisan restart`
  before it existed) → patched by adding the commands and a prompt rule.
- Stale gmail-failure claims repeated as current → patched by retiring
  the claims and adding `skill_auth` to self_state.
- "Task processor stalled from that database lock yesterday" (2026-07-06)
  → the sleeping-Mac incident: a self-authored claim stored at
  **confidence 1.0** with basis "direct observation of its internal logs"
  (it was no such thing) was retrieved the next morning, narrated as
  current fact, relayed by the owner to a second agent, and ended with a
  healthy process being killed. Patched by retiring the claim and making
  self_state sleep-aware.

The patches are real and stay. But new instances will keep appearing
until the two class-level seams below exist.

## 2. Settled design (implement, do not relitigate)

### 2.1 Seam A — self-questions route through ground truth
1. Add a deterministic detector (keyword/pattern, not LLM) for
   self-referential turns: questions about what the agent can do, its
   commands, system status, service health, auth state, scheduled tasks,
   or its own recent actions. Cheap and over-inclusive is fine — the cost
   of a false positive is a few hundred extra tokens of truth.
2. On detection, the conversation turn injects the RELEVANT ground-truth
   block *before* the model answers: `render_self_state(...)` for
   status/schedule/auth questions; the relevant section of the generated
   capabilities manifest for capability/command questions. The model does
   not get to choose whether to call the tool on these turns — the truth
   is already in front of it. (The `self_state` tool stays, for turns the
   detector misses.)
3. Prompt rule (one addition, not a rewrite): when a GROUND_TRUTH block
   is present, statements about the agent's own state may come ONLY from
   it; retrieved memory about the agent's own past state is history, to
   be cited as history ("on July 5 I reported X") and never as current.

### 2.2 Seam B — self-reports can never impersonate observations
The poison record worked because nothing distinguished "the agent said
this about itself" from "this was observed." Fix at capture time, in the
writer/skeptic path:
1. Any claim whose subject is the agent's own operational state gets
   `claim_class: self_report`, and its confidence is CAPPED at medium.
   `confidence: 1.0` with `owner: agent` about its own internals must be
   structurally impossible (schema gate + test, like the tokenization
   gates).
2. A `confidence_basis` may assert observation ("read from logs", "tool
   output") only when the capture carries a linked tool result. Otherwise
   the basis is written as "agent self-report, unverified".
3. Retrieval renders `self_report` claims with a one-line banner:
   `[agent self-report from <date> — for current state, self_state]`.
   The banner is generated at the rendering layer (see the lean-retrieval
   lesson: never regex it in afterwards).

### 2.3 What NOT to do
- Do not hardcode answer templates or intercept the turn away from the
  model — the model still writes the reply; it just writes it looking at
  the truth.
- Do not delete or block memory about the agent's own history. History
  is legitimate autobiography; the defect is only its impersonation of
  the present.
- Do not make the detector an LLM call. It runs every turn; it must be
  deterministic, testable, and free.

## 3. Definition of done
- Detector unit-tested against the case history above (each past
  incident's trigger phrasing must detect).
- A conversation-level test: with a stale self-claim in the vault AND a
  healthy self_state, a "what's your status?" turn produces an answer
  consistent with self_state (mock the LLM; assert the ground-truth
  block, not the wording).
- Schema gate test: constructing an agent-owned operational claim with
  confidence above medium fails.
- The 2026-07-06 scenario replayed end-to-end as a regression test: the
  retired stalled-processor claim re-seeded in a test vault must arrive
  at the model wearing its self-report banner.

---

*Origin: distilled 2026-07-06 from the honesty critique ("the whack-a-
mole pattern with Jake's confabulations") and the sleeping-Mac incident
of the same day, in conversation between August and Claude (Fable 5).
The instances were patched as they appeared; this is the class.*
