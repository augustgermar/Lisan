# Ship 2 — closing its own knowledge gaps

*Status: specified, not built. Supersedes the 2026-07-05 design note of the
same name, which was rewritten on 2026-08-14 after the owner read it back
against its own intent. `enrich_entity` / `enrich_person` remain registered in
`action_policy.ACTION_TIERS` at tiers 2 and 3; the tier is the owner's on
switch and the last step, as before.*

## §0 — Intent

The system notices where its model of the world is thin, goes and finds what
would fill the gap, and writes down what it learned. That sentence is the
whole feature. Ship 1 built the noticing. Ship 2 builds the going.

## §1 — What changed, and why the previous version could never have worked

The superseded note specified a four-prong permission gate, a source-class
allowlist, a `frame` field on the open loop, and a fail-closed default. Two of
its own rules, read together, made the feature inert:

> *"Detector-emitted person loops (`origin: self`) carry `frame: none` by
> construction — the detector cannot know why a person matters."*
> *"`frame: none` means prong 3 fails, which means surface-to-owner is the ONLY
> closure path."*

Every loop Ship 1 emits is `origin: self`, therefore `frame: none`, therefore
permitted to do nothing but ask the owner — which is what v0 already did. The
gate would have been built, tested, shipped, and never once fired on the loops
the detector actually produces. All 26 loops from the calibration period fall
in that class.

The prongs, the allowlist, and the `frame` field are removed. What replaced
them is not a weaker gate; it is no gate. The three pieces of that note which
were never gates — provenance, inference discipline, and retention for fetched
third-party material — are kept below, because they are about *what the system
does with what it finds*, not about whether it is allowed to look.

**A spec whose main content is denial conditions produces nothing.** That is
the lesson worth carrying out of this rewrite.

## §2 — The loop

A `thin` deviation is the trigger. Ship 1 already produces it, in the owner's
words (real example, cast renamed): *"Ruth Varga keeps coming up (71 records)
but my model of them is 18 words — there is clearly more to know."*

1. **Name the deficit.** Not "learn about X" but "X appears in 71 records and
   the entity narrative is 18 words." The deficit is the search target and the
   stopping condition. Appetite without a named deficit is the failure mode.
2. **Ring 0 — the vault.** Search interior memory first. The material is often
   already captured and simply never reached the entity's story. Free, and it
   frequently ends here.
3. **Ring 1 — the owner's own corpora.** Gmail, Obsidian, the owner's files,
   through the existing skills. Scoped to the named deficit; never a background
   sweep.
4. **Write the resolution, not the corpus** (§4).
5. **Close the loop and say what was learned.** One line to the owner: what it
   didn't know, what it now knows, where that came from.
6. **If the gap does not close, ask — informed.** Rings 0 and 1 run *before*
   the question so the question arrives with what was found: *"your notes say
   Tuesdays and a March email says Wednesdays — which is current?"* A question
   only the owner can answer is still asked, with curiosity, not silently
   resolved.

Satiation is inherited from Ship 1: the deviation resolves, the fingerprint
stops re-firing, and the system goes quiet. The mosquito seeks one specific
meal, then stops.

## §3 — The source ladder

- **Ring 0 — the vault.** Interior. Always first. No conditions.
- **Ring 1 — the owner's own corpora.** The owner's data, read on the owner's
  machine, for the owner's benefit. No third-party question arises. Two
  disciplines, neither of which can refuse a lookup: every read is scoped to a
  named deficit, and every acquisition carries provenance (§4).
- **Ring 2 — the published world.** **Deferred, not designed here.** There is
  currently no published-class fetch surface installed: `arxiv_search`, `maps`
  and `polymarket` were removed on 2026-08-14, leaving Gmail, Obsidian and
  `youtube_transcript`. Ring 2 needs one open decision before it is built (§9)
  and is out of scope for this ship.

Escalation is outward only when the inner ring fails.

## §4 — What gets written

**The resolution, never the corpus.** An email thread is not copied into the
vault. What lands is what was learned — *"Ruth Varga is Dana Feld's mother"* —
with a pointer to where it came from. The vault is a memory, not a mail
archive, and the owner's corpora are already searchable where they live.

**Provenance on every acquisition.** Enrichment appends to the entity's
durable `source_log`, the same seam birthdays use, so it survives narrative
compaction and is searchable immediately:

```
[enrichment] 2026-08-14 — learned <what> from <ring>/<skill> while closing
             <loop_id>
```

Nothing is written that cannot say where it came from. Without this, an entity
file asserts things with no way to distinguish what the owner said from what
the system went and read — the confabulation problem in a new costume.

**Inference is marked and capped.** A statement the system *derived* rather
than read carries `basis: inference` and confidence capped at 0.6, matching
the `self_report` cap from WO-GROUND. Any direct evidence about the subject
supersedes an inference rather than sitting beside it. Guesses must not harden
into facts by aging.

**Retention.** Ring 0 and Ring 1 findings are permanent: they are facts about
the owner's own life, learned from the owner's own data, and expiring them
would be absurd. Corrections use the ordinary record lifecycle. The loop-scoped
expiry and tombstone cascade of the superseded note were designed for fetched
third-party material and belong with Ring 2 when it is built.

## §5 — Budgets, which are not gates

A cap is a resource budget, not a permission test: it never decides that a
particular subject may not be looked into, only how much work runs per day.

- `enrichment.daily_cap` — enrichment attempts per day. Start at 2, matching
  Ship 1's `daily_cap`, which never bound (0.65/day actual over 40 days).
- `enrichment.max_reads_per_loop` — corpus reads while closing one deficit.
  Start at 5. Prevents a single thin entity from turning into a mailbox scan.
- Every attempt is logged whether or not it found anything, so a system that is
  trying and failing is visible rather than quiet.

## §6 — The owner's switch

`policy_tier` stays exactly as it is: `enrich_entity` at 2, `enrich_person` at
3, and the clamp raised in code as the final commit. Setting the tier on the
live install remains the owner's manual act. The agent ships the capability;
the owner turns the key. This is the one place a "no" lives, and it is a single
switch the owner holds rather than a per-subject judgement in the hot path.

## §7 — Implementation, in order

1. **`enrichment.seek` job + Ring 0.** Deficit extraction from a `thin`
   deviation, vault search, write-back with provenance, loop closure. This is
   the whole feature end to end against interior memory, and it is testable
   with no external calls at all.
2. **Provenance and the audit line** (§4), on the `source_log` seam.
3. **Inference marking and the 0.6 cap**, plus supersede-on-direct-evidence.
4. **Ring 1 adapters** — Gmail and Obsidian, deficit-scoped, read-limited.
5. **Budgets and logging** (§5).
6. **Raise the `policy_tier` clamp to 3.** Last commit, after everything above
   is green.

Step 1 is deliberately a complete feature. If Ring 1 never shipped, an agent
that reconciles its own entity stories against its own memory would still be
worth having.

## §8 — Definition of done

- Full suite green, both runners.
- Ring 0 enrichment tested end to end: deviation in, entity updated, loop
  closed, provenance line present.
- Provenance asserted on every write path — a test that fails if an enrichment
  can land without saying where it came from.
- Inference cap and supersede-on-evidence tested.
- Live dry-run against a disposable vault (`LISAN_VAULT=/tmp/...`), never
  against `~/.lisan/vault` without asking.
- `config.example.json` ships with tier 3 **not** set.
- The owner sets the tier.

## §9 — Open decisions

**Ring 2 subject classes.** Before any published-world lookup is built: is
there a class of subject the system never researches on the open web? The
vault contains the owner's children and people in an active custody matter,
and "an agent doing web research on a seven-year-old" is a different sentence
from "an agent reading its owner's email." Raised 2026-08-14, deliberately
unanswered here rather than decided by whoever writes the code. **Blocks Ring 2
only; Rings 0 and 1 proceed.**

**A published-class source.** Ring 2 also needs at least one installed skill
that reaches published material, since the candidates were uninstalled.
