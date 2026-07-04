# Phase 2 runbook — status ledger

Runbook: `docs/phase2_roadmap.md` (commit 221c8fc). One line per work
order; update state transitions with date + completing commit.

| WO | Title | State | Date | Commit |
|----|-------|-------|------|--------|
| 0 | Preconditions and pre-work | done | 2026-07-04 | (this) |
| 1 | Kernel mechanics (gate, hash, voice injection) | done | 2026-07-04 | (this) |
| 2 | Voice extraction pass + ratification ceremony | pending | — | — |
| 3 | Consistency rubric, instrumentation, baseline | pending | — | — |
| 4 | Layer B: self-episodes + capability beliefs | pending | — | — |
| 5 | Deficit scorer + session-open callbacks | pending | — | — |
| 6 | Self-belief reconciliation dreamer job | pending | — | — |
| 7 | Graduated action policy | pending | — | — |
| 8 | Wipe Test (clone-only) | pending | — | — |
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
