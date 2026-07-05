# Belief formation (WO-10 spec — approved, not yet built)

Owner decision 2026-07-05: capability-belief formation follows the
**dreamer-proposes, owner-approves** model, mirroring the voice ceremony.
This document is the work-order spec. Nothing here is implemented yet;
`lisan dreamer reconcile` remains a correct no-op until it lands.

## Why formation is gated

Revision (`lisan dreamer reconcile`, WO-4/WO-6) is already evidence-chained:
a belief can only move when episodes contradict it, and fabricated evidence
is rejected at the gate. Formation is the remaining confabulation surface —
a belief formed from thin or misread evidence *becomes the agent's
self-story*, and revision can only walk it back as fast as contradicting
evidence accumulates. So creation gets the same ceremony treatment as the
kernel voice: deterministic extraction, hard evidence gate, owner
ratification, provenance stamped.

## Command surface (mirrors the voice ceremony)

```bash
lisan self extract-beliefs            # deterministic pass over self-episodes
                                      # → candidates artifact in reports/
lisan self ratify --from <artifact>   # existing ceremony command; grows a
                                      # --kind beliefs mode (or dispatches on
                                      # the artifact's declared kind)
```

No `--provisional` path for beliefs: unlike voice (where the agent needed a
working register before the owner reviewed it), an empty belief ledger is a
safe default. Beliefs enter the ledger owner-ratified or not at all.

## Extraction pass

Input: `self/episodes/` records only (Layer B; deterministic, source_refs
mandatory — the property that makes fabricated evidence structurally
impossible carries through to formation).

Candidate rule: a belief candidate is a generalization over episode
*outcomes* of the same kind — plan completions, job success/failure classes,
capability demonstrations ("drafted and sent an email via gmail_send after
approval"), recurring failure modes ("codex tasks touching PDF parsing fail").

Evidence gate (all required, thresholds configurable under
`self.belief_formation`):

- ≥ 3 supporting self-episodes, spanning ≥ 2 distinct days
- every cited episode must exist and carry `source_refs` (verified at
  extraction AND re-verified at ratification, same as voice quotes)
- counterexample scan: episodes contradicting the candidate are listed on
  the candidate, not hidden; a candidate with contradiction ratio > 1/3 is
  dropped by the extractor
- **eval-tagged history is excluded**: episodes whose conversation ids match
  the eval namespaces (`eval-*`, `scale-*`, `cap-*`, `grow-*`) or that fall
  inside a declared timeshift window do not count toward the gate. Beliefs
  must come from real use, not from how the agent behaves in rehearsals.

Caps: initial ratification set ≤ 7 beliefs; formed beliefs start at
confidence "medium" at most (confidence must be earned through reconcile
cycles, never granted at birth).

## Artifact and ledger

The extraction artifact is a `report` record (like
`voice-extraction-*.md`): stats, thresholds, candidates with full evidence
lists and counterexamples. Ratification writes each approved candidate via
the existing `new_self_belief()` (`self/beliefs/`), with:

- `provenance: "formed"` plus the artifact id
- the evidence episode ids as the belief's opening evidence chain
- `ratified_by: "owner"` and date

Formed beliefs are immediately subject to `lisan dreamer reconcile` — the
first reconcile after formation is a good smoke test (it should be a no-op
against the very evidence that formed them).

## Adjacent deliverable: self-story retrieval breadth

Bundled into this WO (owner decision 2026-07-05) because it touches the
same self-record retrieval surface: open-ended identity questions ("tell
me about yourself", "what have you been through") currently answer tersely
from the primer instead of weaving the Layer B autobiography. Fix shape:
when the query is self-referential, bias retrieval toward `self_episode`
(and, once formed, `self_belief`) records so the conversation prompt can
weave them. One eval probe set covers both deliverables: belief candidates
form correctly AND the agent tells its own story with breadth.

## Non-goals

- No LLM in the loop for the gate itself (an LLM may *phrase* candidate
  statements, but eligibility is deterministic — same split as voice).
- No auto-ratification, no scheduled formation. The extractor may be run by
  the dreamer on a cadence, but its output is always a queued artifact for
  the owner.
- No beliefs about the *owner* (that is the entity/pattern system's job);
  the ledger is strictly self-capability.
