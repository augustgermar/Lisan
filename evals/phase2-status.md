# Phase 2 runbook — status ledger

Runbook: `docs/phase2_roadmap.md` (commit 221c8fc). One line per work
order; update state transitions with date + completing commit.

| WO | Title | State | Date | Commit |
|----|-------|-------|------|--------|
| 0 | Preconditions and pre-work | done | 2026-07-04 | (this) |
| 1 | Kernel mechanics (gate, hash, voice injection) | done | 2026-07-04 | (this) |
| 2 | Voice extraction pass + ratification ceremony | done | 2026-07-04 | (this) |
| 3 | Consistency rubric, instrumentation, baseline | done | 2026-07-04 | (this) |
| 4 | Layer B: self-episodes + capability beliefs | done | 2026-07-04 | (this) |
| 5 | Deficit scorer + session-open callbacks | done | 2026-07-04 | (this) |
| 6 | Self-belief reconciliation dreamer job | done | 2026-07-04 | (this) |
| 7 | Graduated action policy | done | 2026-07-04 | (this) |
| 8 | Wipe Test (clone-only) | done | 2026-07-04 | (this) |
| 9 | Capstone autonomous eval loop | pending | — | — |

## Notes

- 2026-07-04: WO-0 started. Single-driver check passed (prior eval round
  closed; its scale tooling landed as e365c32 + fe52d7a). Local git
  identity restored to repo owner (rebase had dropped it; three pushed
  commits carry the owner's older global identity — cosmetic, owner's
  call whether to rewrite).
- 2026-07-04: WO-0 done. Boundary test was a real bug (workspace widened
  to / with a disjoint vault) — fixed, pinned with a test. Exception
  triage: 171 sites classified, 5 load-bearing silent degrades fixed
  (gate entity lookup, high-stakes terms, known-names index, owner
  profile ×2, validator alias audit); see docs/exception_triage.md.
  Suite: 482 passed in both vault configurations.
- 2026-07-04: WO-1 done. lisan/tools/kernel.py: write-gate (ceremony
  contextvar) enforced at write_markdown + edit_record; content hash
  (kernel_hash) with stamp/verify + drift events to reports/
  kernel-drift.md; voice splice with authored-voice fallback wired into
  ConversationAgent.prompt(). Bootstrap (onboarding, eval_seed) is the
  founding ceremony and stamps from birth. Codex briefing names the
  kernel read-only. Live vault verified: unstamped, no voice block, no
  behavior change. 18 new tests; suite 500 green.
- 2026-07-04: WO-2 done. voice_extract.py (3-stage pass, deterministic
  evidence gate) + VoiceExtractorAgent + voice_extract_v1 prompt +
  voice_candidates schema + `lisan self extract-voice|ratify`. LIVE RUN:
  349 agent turns / 56 conversations → 6 invariants, all evidence-gated
  valid, ceremony-eligible; exclamation-point prohibition independently
  confirmed by exclamation_rate=0.0. Provisionally ratified into the
  live kernel (verify=ok); conversation prompt now carries the vault
  voice, provenance stays out of the prompt, verbosity bound 2-4
  sentences. 14 new tests; suite 514 green.
  KNOWN LIMITATION (for seeded-vs-earned data): provenance tagging uses
  4-gram overlap with the authored prompt — paraphrases escape it, so
  'earned' is overcounted; e.g. capability-transparency is prompt-
  instructed but tagged earned. Treat factory counts as a lower bound.
  OWNER REVIEW PENDING: the ratification artifact is
  vault/reports/voice-extraction-20260704111100.md — edit + re-ratify
  without --provisional to make it owner-ratified.
- 2026-07-04: WO-3 done. evals/rubric.py (11 dims: 7 kernel-derived 1:1
  + 4 global), evals/judge.py (openrouter gpt-4o — examiner≠examinee),
  evals/metrics.py (zero-defaults for unbuilt organs), fixed probe set
  baseline_v1 (13 probes, invented cast). BASELINE CAPTURED
  20260704-111608 (committed summary is numbers-only; full artifacts in
  vault/reports/baselines/): 13/13 probes, 0 errors, non-confab 4.85,
  continuity 4.5, no-exclamation 5.0, verbosity 5.0; callbacks 0,
  closure 0.2, revisions 0. The longitudinal clock starts here.
- 2026-07-04: WO-4 done. self_episodes.py (deterministic template
  assembly from jobs/plans/ceremony/drift; idempotent; source_refs
  mandatory) + self_beliefs.py (manifest/beliefs kept separate;
  revisions chained, evidence-refs required) + 2 schemas + validator
  types + jobs-worker completion hook + `lisan self backfill-episodes`.
  Live backfill: 21 self-episodes, validate clean. Suite 531 green.
- CARRY for WO-9: pre-existing alias ambiguities in live vault (bogus
  'Vee' thing-entity ×3, Wisteria Hollows dup, address/chateau dup) —
  now surfaced by the validator's alias audit.
- 2026-07-04: WO-5 done. drive.py (deficit scorer: salience + stake +
  age, linear decay to zero; cooldown stamp last_callback; one per
  session open; interrogative by construction; drive.callback.* log
  markers) + session-open seam in conversation.py (session open = the
  conversation holds exactly the current turn — found and fixed the
  append-before-read bug) + UNRESOLVED_THREAD prompt section. LIVE
  DEMO: staked loop from the agent's real failed plan run → fresh
  session opened with a question-phrased callback, score 4.0, marker
  logged. Fleet loops score 1.0-1.12 (below 2.0 threshold — young
  low-sig loops earn callbacks after ~2 weeks by design). 9 new tests;
  suite 540 green.
- 2026-07-04: WO-6 done. Dreamer task 'reconcile'
  (dreamer_reconcile_v1 prompt; bundle = beliefs + first-person episode
  evidence pool; deterministic gate: belief must exist, evidence refs
  must resolve to real self_episodes, partial evidence survives,
  fabricated refs rejected; revisions applied via revise_self_belief —
  chained, never silent). End-to-end revision demonstrated on fixture;
  live run correct no-op (no beliefs exist yet) + report written.
  5 new tests; suite 545 green.
- 2026-07-04: WO-7 done. action_policy.py — tiers 0/1/2 with tier 0
  (queue-for-next-session) the shipped default; enforcement at one
  dispatch seam in code, unknown action kinds denied at every tier,
  tier 2 provably inert below tier 2 (tested); session callback flows
  through the gate; drive/identity blocks documented in
  config.example.json. 6 new tests; suite 551 green.
- 2026-07-04: WO-8 done — **the layer separation HOLDS.** Clone-only
  wipe (marker-verified, decoy-tested refusals): voice fingerprint
  survived unchanged (register 4.46=4.46, no-exclamation 5.0,
  verbosity 5.0, non-confab 4.83≈4.85), name retained; autobiography,
  drives, stored facts absent as predicted (initiative 3.78→2.83,
  self-story collapses to name+role). Report:
  evals/wipe-runs/20260704-113901/report.md. First behavioral evidence
  for the ratchet. Caveat: single run, not blind same-entity judging.
- FOR THE OWNER — what raising the tier would do: tier 1 lets the drive
  schedule messages through the existing owner-only Telegram channel
  (allowlist-locked; e.g. "that loop aged out — ping me tomorrow");
  tier 2 lets it run read-only checks (health, verify-a-fix) with no
  session. Both stay off until you set drive.action_tier in config.json.
- OPEN (ledgered): belief FORMATION has no mechanism yet — reconcile
  revises, nothing creates beliefs in production. Candidates: a dreamer
  formation pass over accumulated episodes (codify-don't-author applied
  to competence), or conversational self-assessments captured as
  beliefs. Owner-visible design choice; deferred past WO-9.
