# Work order: enable Ship 2 (person enrichment)

*Hand this file to the coding agent when the owner decides the calibration
period is over. It is deliberately not executable without the owner's
calibration verdict pasted into the slot below.*

Read `docs/ship2_person_enrichment.md` — it is the binding spec. Also read
`lisan/tools/action_policy.py`, `lisan/tools/deviations.py`, and
`lisan/tools/drive.py` before writing anything.

**Rewritten 2026-08-14.** The previous version of this work order told the
agent to build a four-prong permission gate. The owner read the spec back
against its own intent and removed it: the gate could never have fired on the
loops Ship 1 actually produces (see §1 of the spec), and a specification whose
main content is denial conditions produces nothing. What the system is for is
noticing where its model is thin and going to find what closes the gap.

Implement the steps in the spec's §7, **in that order**. Raising the
`policy_tier` clamp so `enrich_person` becomes reachable is the last one and
must be the final commit. If you find yourself editing the clamp before Ring 0
enrichment, provenance, and the inference cap exist with passing tests, stop —
you are doing it in the wrong order.

Hard rules that override anything else you infer:

- **the resolution is written, never the corpus** — an email thread does not
  get copied into the vault; what was learned does, with a pointer to where it
  came from;
- **nothing lands that cannot say where it came from** — provenance on every
  write path, on the durable `source_log` seam;
- **inference is marked and capped** (`basis: inference`, 0.6) and is
  superseded by direct evidence rather than sitting beside it;
- **every read is scoped to a named deficit** — background scanning of any
  corpus is never the behaviour; appetite without a loop is the failure mode;
- **Ring 2 is out of scope** and blocked on the open decision in the spec's §9;
- caps are budgets, not permission tests: they bound how much work runs, never
  which subject may be looked into.

## Owner calibration verdict (REQUIRED — agent: stop and ask if blank)

> **Curiosity, not grinding.** — August, 2026-08-14
>
> The surfaced questions read like a mind being curious, and the system
> working as it was intended. Reviewed against the full calibration period:
> 40 days, 26 origin:self loops, 0.65/day against a daily_cap of 2 — it
> never once reached its own ceiling. 22 of 26 resolved.
>
> The classes it found: 10 probable-duplicate records, 8 "this person comes
> up constantly and I barely know them", 5 self-diagnostics on its own
> broken machinery, 2 quality-slippage reads, 1 cross-kind collision. The
> self-diagnostics were correct — the embedding-backlog ache was true, and
> that lane later turned out to have been dead for the life of the install.
>
> **Dials: unchanged.** daily_cap stays 2, thin_person_max_words 25,
> thin_person_min_mentions 3, stale_after_days 30, failed_jobs_threshold 5,
> embedding_backlog_threshold 25; drive cooldown_days 7, min_score 2.0,
> max_callbacks 2. Nothing was tuned because nothing bound: the cap was
> never hit, so lowering it would change nothing and raising it would be
> speculation about pressure that never arrived. Revisit if Ship 2's own
> enrichment loops push the rate up.
>
> Calibration period closed 2026-08-14. Proceed with the six steps.

## Definition of done

- Full suite green, both runners.
- Ring 0 enrichment tested end to end: deviation in, entity updated, loop
  closed, provenance line present.
- A test that fails if an enrichment can land without provenance.
- Inference cap and supersede-on-direct-evidence tested.
- Live dry-run against a disposable test vault (`LISAN_VAULT=/tmp/...`), never
  against `~/.lisan/vault` without asking the owner.
- Only then: the clamp raise, plus a config example showing tier 3 NOT set
  by default.
- **Enabling on the live install (setting the tier) is the owner's manual
  act. The agent ships the capability; the owner turns the key.**
