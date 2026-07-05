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

> [owner: paste your mosquito-vs-indexer read here — a few sentences on
> whether Ship 1's surfaced questions felt like curiosity or grinding, and
> any dial adjustments you want (daily_cap, thresholds).]

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
