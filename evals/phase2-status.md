# Phase 2 runbook — status ledger

Runbook: `docs/phase2_roadmap.md` (commit 221c8fc). One line per work
order; update state transitions with date + completing commit.

| WO | Title | State | Date | Commit |
|----|-------|-------|------|--------|
| 0 | Preconditions and pre-work | in-progress | 2026-07-04 | — |
| 1 | Kernel mechanics (gate, hash, voice injection) | pending | — | — |
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
