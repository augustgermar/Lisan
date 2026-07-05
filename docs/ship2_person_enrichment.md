# Ship 2 design note — person enrichment, gated but disabled

*Follow-up to WO-ENRICH §3/§4. Status: specified, NOT enabled. `enrich_person`
is registered in `action_policy.ACTION_TIERS` at tier 3, which `policy_tier`
cannot reach (it clamps to 2) — structurally unreachable until this note's
gate is implemented and the ceiling is raised on purpose, in that order.*

*Ship 1 (deviation detector, interior-only) shipped 2026-07-05. The
calibration period on real data starts now; Ship 2 work does not begin until
that period has produced a written mosquito-vs-indexer read.*

---

## §4.1 — Machine-decidable `disclosure_intent`

**Decision: a source-class allowlist that fails closed.** "Published to the
world" is not decidable from a URL in general; it IS decidable for an
enumerated set of source classes. Config gains `enrichment.sources`, an
allowlist of classes with fixed disclosure semantics:

- `published`: preprint servers/DOIs, an organization's own public site, official
  business listings, published docs. The existing skills platform (e.g.
  `arxiv_search`) is exactly this class — reuse skills as the only fetch
  surface, so what CAN be reached is enumerable from the skill manifest.
- everything else: `bounded`, by definition, including anything behind a
  login, any social platform (public-by-default platforms included — a
  public post is world-readable but audience-scoped in intent), and any
  general web search over a person's *name*.

**Aggregation prong, mechanically:** one loop closure may consult at most
one source class per acquisition (config `enrichment.max_sources_per_loop:
1`). Cross-source assembly of a person profile is the aggregation violation
(§1.3.2) and is prevented by the counter, not by judgment.

Ambiguous → `bounded` → denied. No judgment call in the hot path.

## §4.2 — Machine-decidable `frame`

**Decision: frame lives on the loop.** An entity outlives frames; a session
is too ephemeral; the loop IS the decision at hand — it already carries
`origin`, links, and lifecycle. Schema addition: `frame: hiring | vendor |
meeting | client | none` on `open_loop`.

- Frame is set only from an explicit owner utterance at loop creation
  ("I'm interviewing X Tuesday") — writer-extracted, skeptic-gated, like
  every other capture field.
- Detector-emitted person loops (`origin: self`) carry `frame: none` by
  construction — the detector cannot know why a person matters.
- **Default = most restrictive**: `frame: none` means prong 3 fails, which
  means surface-to-owner ("I know almost nothing about X — anything I
  should track?") is the ONLY closure path. This is v0's whole behavior,
  and it stays the behavior for any loop without an owner-given frame.

Off-frame fact detection at acquisition time: the fetch is scoped by the
frame→source-class map (hiring → `published` professional classes only),
so an off-frame fact is mostly unreachable rather than filtered after the
fact. Prevention over filtering, same as the confabulation stance.

## §4.5 — Retention trigger and forgetting cascade

**Decision: the trigger is the originating loop leaving `active`.** The loop
is the decision; when it resolves, expires (drive decay), or is archived,
retention ends. Acquired records are born tagged:

```
acquired_for: <loop_id>
acquired_at: <date>
evidence_class: published-source
```

On loop close, an `enrichment.expire` job (same queue, deterministic):

1. deletes acquired records tagged with that loop id;
2. cascades to inferences derived from them (any record whose `links` /
   `source_refs` cite a deleted acquired record) — delete or demote to
   `status: retracted`;
3. **tombstones, never orphans**: each deleted record is replaced by a
   one-line tombstone record ("acquired content expired per retention
   discipline, <date>") so surviving `source_refs` resolve to the fact of
   expiry rather than dangling — and the deviation detector's dangling-link
   scan stays quiet;
4. the acquisition self-episode is kept but references only the tombstone:
   the agent remembers THAT it looked and why, never retains WHAT it found.
   No standing dossier, but an honest autobiography.

## §4.6 — Rollback for misclassification

If an acquisition is later judged to have breached the gate (owner says so,
or a source class is reclassified):

1. run the §4.5 expiry cascade immediately for that loop (blast radius =
   everything tagged `acquired_for` + derived inferences, enumerable by
   construction — this is why the tag is mandatory at birth);
2. write an audit-visible retraction record in `reports/` naming what was
   accessed, when, under which class, and why that was wrong;
3. emit a self-episode (biography-grade: the agent was wrong about a
   boundary — that is exactly the kind of event Layer B must carry);
4. the loop reopens as `frame: none` → surface-to-owner only.

## What Ship 2 implementation requires, in order

1. Calibration read from Ship 1 (mosquito or indexer?) — written, owner-read.
2. The four prongs as pure functions over (loop, entity, source class) with
   the full test matrix, including the §1.4 rule that person-protections do
   not thin with incidental origin.
3. `enrichment.expire` + tombstones + cascade, tested.
4. Audit line format in the entity file (§1.5) + provenance via the existing
   evidence-class fields.
5. Inference immune system (§1.6): inference records get `basis: inference`,
   capped confidence, and demotion-by-individual-evidence in the skeptic.
6. Only then: raise the `policy_tier` clamp to 3 and let the owner set it.

*Symmetry test stays the tie-breaker for anything this note underdetermines:
would the subject find it fair if a competent agent did it to them for
someone else? If not, it fails — regardless of what the prongs technically
permit.*
