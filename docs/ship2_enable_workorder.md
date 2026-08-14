# Work order: enable Ship 2 (person enrichment)

*Hand this file to the coding agent when the owner decides the calibration
period is over. It is deliberately not executable without the owner's
calibration verdict pasted into the slot below.*

Read `docs/ship2_person_enrichment.md` — it is the binding spec, written
against this codebase. Also read `lisan/tools/action_policy.py`,
`lisan/tools/deviations.py`, and `lisan/tools/drive.py` before writing
anything.

Implement the six steps in the spec's "What Ship 2 implementation requires,
in order" section, **in that order**. Do not skip ahead: raising the
`policy_tier` clamp so `enrich_person` (tier 3) becomes reachable is step 6
and must be the final commit, after everything before it is tested and
green. If you find yourself editing the clamp before the four-prong gate
functions, the `enrichment.expire` tombstone cascade, the audit-line format,
and the inference immune system all exist with passing tests, stop — you are
doing it in the wrong order.

Hard rules that override anything else you infer:

- the gate fails closed — ambiguous source = bounded = denied;
- a person never loses person-protections because they entered the vault
  incidentally (§1.4 of the WO);
- detector-emitted loops are `frame: none` and may only surface-to-owner;
- forgetting must tombstone, never orphan `source_refs`;
- every acquisition is audit-logged in the entity file;
- the symmetry test is the tie-breaker for anything the prongs
  underdetermine: would the subject find it fair if a competent agent did
  this to them for someone else?

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

- Full suite green.
- Prong functions have a test matrix including the incidental-person rule.
- `enrichment.expire` tested, including the derived-inference cascade and
  tombstones.
- Live dry-run against a disposable test vault (`LISAN_VAULT=/tmp/...`),
  never against `~/.lisan/vault` without asking the owner.
- Only then: the clamp raise, plus a config example showing tier 3 NOT set
  by default.
- **Enabling on the live install (setting the tier) is the owner's manual
  act. The agent ships the capability; the owner turns the key.**
