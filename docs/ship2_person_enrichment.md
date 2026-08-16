# Ship 2 — closing its own knowledge gaps

*Status: PARTIALLY IMPLEMENTED, active queue. Supersedes the 2026-07-05
design note of the same name, which was rewritten on 2026-08-14 after the
owner read it back against its own intent. The bounded `enrichment.seek`
core, provenance write-back, retry path, and scoped transcript lane are
implemented and tested; the historical-transcript decision is resolved. Owner
clarification handling and the final enrichment-tier decision remain open. Ring
1 adapters are implemented behind the core provider interface, and
the first audit rollup is now present. `enrich` is registered in
`action_policy.ACTION_TIERS` at tier 3; the tier is the owner's on switch and
the last implementation step.*

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
2. **Search the current transcript.** The answer may have been provided only a
   few turns earlier. Search the active conversation and its narrative state
   before invoking any broader retrieval or model call.
3. **Search historical transcripts.** Search the transcript index for direct
   wording, clarifications, corrections, and owner statements that the
   distillation pipeline may not have carried into the entity story.
4. **Ring 0 — the vault.** Search interior memory after transcripts. The
   material is often already captured and simply never reached the entity's
   story. Free, deterministic, and frequently sufficient.
5. **Ring 1 — indexed owner-controlled sources.** Search Gmail, Obsidian, and
   explicitly configured local-file roots through deterministic native or
   platform search surfaces. Query the indexes; do not scan every file or
   repeatedly send an entire corpus to a model. Every read remains scoped to
   the named deficit.
6. **Ask the owner when that is the highest-value next step.** If the owner is
   likely to know, can determine whether the question matters, can resolve a
   conflict, or can decide whether external research is appropriate, surface
   an informed question in chat. Include what was found and what remains
   uncertain; never ask a generic "tell me more" question.
7. **Ring 2 — the published world, when authorized.** If the question remains
   worthwhile and unresolved, the future web-research adapter may use the
   installed `research` skill's source discipline. Ring 2 remains deferred and
   blocked by §9.
8. **Write the resolution, not the corpus** (§4), then close the loop and say
   what was learned, what remains uncertain, and where it came from.

The owner is an information source, not an infallible oracle. An owner answer
must be represented as an owner statement with transcript provenance, and must
be distinguished from an observation, interpretation, preference, correction,
or boundary. The owner may also answer that a question is not worth pursuing,
that it must not be researched, or that uncertainty should remain open.

Satiation is inherited from Ship 1: the deviation resolves, the fingerprint
stops re-firing, and the system goes quiet. The mosquito seeks one specific
meal, then stops.

## §3 — The source ladder

- **Transcript 0A — the current conversation.** Search the active transcript
  and narrative state. This is the cheapest and most immediate source.
- **Transcript 0B — historical conversations.** Search the separate,
  enrichment-only transcript lane for direct owner statements and
  clarifications before searching derived records. The lane discovers local
  transcript files, stores a private sidecar with content and embedding hashes,
  and uses bounded lexical-plus-embedding ranking. It does not add transcripts
  to ordinary retrieval.
- **Ring 0 — the vault.** Search entities, episodes, evidence, claims,
  decisions, knowledge, open loops, and explicit links using the existing
  deterministic retrieval lanes. Transcripts have precedence when their direct
  wording answers the question.
- **Ring 1 — indexed owner-controlled corpora.** Gmail, Obsidian, and local
  files are searched through native APIs or persistent local indexes. Local
  files are limited to user-configured roots; no username, operating-system
  layout, home-directory assumption, or whole-disk scan may be embedded in the
  code. A configured root may use the platform's generic home-directory
  expansion (for example `~`), but must be explicitly enabled by the user.
  Every read is scoped to the named deficit and every acquisition carries
  provenance (§4).
- **Ring 2 — the published world.** **Deferred, not designed here.** Ring 2
  needs the open decisions in §9 and is out of scope for this ship.

Escalation is outward only when the inner ring fails.

### Deterministic retrieval rule

Source discovery and candidate selection should be code, not an LLM task:

```
indexed query → bounded candidate excerpts → selective full read →
evidence extraction → model synthesis only when interpretation is required
```

Native search, FTS, phrase matching, date filters, deduplication, source
ranking, and result caps must run without a model call. The model receives only
the bounded candidate material and the named deficit, never an unbounded corpus.

**The embedding lane is deterministic and must be used.** "No model call" means
no LLM call. Semantic search here is `fastembed` — a local ONNX model running
in-process, no network, no tokens, no provider — so it belongs in the list
above beside FTS and phrase matching, not in the synthesis step. A deficit like
"I know 18 words about someone who appears in 71 records" is exactly the query
that lexical matching answers badly and vector similarity answers well.

Concretely: vault and transcript search go through the **existing three-lane
RRF fusion** (`retrieval.retrieve_context` — SQL + FTS5 + embeddings), never a
bespoke query written for enrichment. That is not a style preference. The
fusion carries anisotropy correction, learned NPMI association edges,
compartment enforcement, the serendipity slot, and demotion of
terminal-status records; a hand-rolled grep silently opts out of all of it and
will quietly retrieve worse than the rest of the system.

Lisan must distinguish read-only search from ingestion. Searching a configured
local root does not import the whole root into the vault; only the compact
resolution and its provenance are written.

### The research interface — core, not a skill

When Ring 2 is built, enrichment must call a **core interface**, not a skill:
`lisan/tools/research.py`, returning a structured finding — source URL, title,
publisher, publication date, retrieval date, excerpt, confidence, disagreement,
and an explicit unverifiable-result field.

Providers sit behind that interface: the installed `research` skill's source
discipline, an MCP server, a browser-driven adapter. This mirrors the pattern
the codebase already uses for models — `lisan/providers/` puts codex, rotato,
local and openai behind one contract — and it is the same reason.

Two arguments settle it against depending on the skill directly:

- **The `research` skill has no code.** It is instruction-only: a `SKILL.md`
  and nothing else, guidance for a model driving the browser. It has no
  callable entry point and returns prose, so it cannot guarantee a single one
  of the typed fields above. A subsystem that needs `publication_date` cannot
  be built on a prompt.
- **A skill can be uninstalled.** Three were removed on 2026-08-14 and the test
  suite went red. If enrichment depends on a skill, uninstalling that skill
  silently disables enrichment — the exact silent-degradation failure this
  release spent a week removing elsewhere. An interface reports "no research
  provider configured"; a missing skill simply never happens, and nothing says
  so.

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

Owner answers use an explicit source type and are not silently promoted to
anonymous facts:

```
source_type: owner_interaction
source_uri: transcripts/<transcript-file>
basis: direct_owner_statement
inquiry_id: <inquiry_id>
```

The statement must also be classified as one of `fact`, `interpretation`,
`preference`, `correction`, or `boundary`. A boundary such as "do not research
this person" closes or redirects the inquiry; it is not evidence about the
person.

Every enrichment attempt records its terminal outcome, including
`resolved_by_owner`, `resolved_by_transcript`, `resolved_by_vault`,
`resolved_by_local_source`, `resolved_by_web`, `owner_declined`,
`owner_marked_not_important`, `conflicting_evidence`, and `unresolved`.

The loop should retain the ring at which it stopped. This makes it possible to
measure whether Lisan is losing information during distillation or reaching
outside unnecessarily.

**Something must read that.** An instrument nobody renders is not an
instrument: the Adjutant logged 638 healthy-looking cycles at `tasks=0` and the
self-evaluation wrote five well-formed reports that measured nothing, both
because the numbers existed and nothing surfaced them. Terminal outcomes and
stop-rings roll up into `self_state` and the weekly report, not only into rows.

**`resolved_by_transcript` is the highest-value outcome in that list, and it is
not an enrichment success.** It means the raw conversation held the answer and
the distillation pipeline dropped it before the entity story. That is a capture
defect report, arriving free, about the writer — and a rising count is a
stronger signal about the health of memory than anything the enrichment itself
produces. Route it back as a deviation against the capture pipeline rather than
filing it as a win.

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
- `enrichment.max_candidates_per_source` — bounded excerpts returned by each
  deterministic source query before selective full reads.
- `enrichment.max_model_calls_per_loop` — synthesis/review calls, separate from
  source reads. Deterministic search must not consume this budget.
- Every attempt is logged whether or not it found anything, so a system that is
  trying and failing is visible rather than quiet.

## §6 — The owner's switch

**One capability, one switch, all entity kinds.** Owner ruling, 2026-08-15:
enrichment does not distinguish a person from a place, a project, or an
organisation. The old `enrich_entity` / `enrich_person` split was an artefact
of the removed gate design and is replaced by one `enrich` action at tier 3.
The old names are not policy actions; callers must migrate to `enrich` rather
than preserve a subject-kind permission distinction.

Tier 3 rather than 2 because the switch should mean the same thing whatever the
subject is, and the stricter reading is the one the owner has always been
described as turning on deliberately. The clamp is raised in code as the final
commit; setting the tier on the live install remains the owner's manual act.
The agent ships the capability; the owner turns the key. This is the one place
a "no" lives, and it is a single switch the owner holds rather than a
per-subject judgement in the hot path.

## §7 — Implementation, in order

1. **`enrichment.seek` job + Ring 0 — core shipped.** Deficit extraction from a `thin`
   deviation, current-transcript search, historical-transcript search, vault
   search, write-back with provenance, and loop closure. This is the whole
   feature end to end against local material, and it is testable with no
   external calls.
2. **Transcript indexing — shipped 2026-08-16.** The owner selected a separate
   enrichment-only lane. It discovers historical transcripts without changing
   ordinary retrieval and records content/embedding hashes in its sidecar.
3. **Deterministic source boundaries.** Add the source-query contract,
   candidate limits, configured local roots, incremental indexing expectations,
   and the rule that search is not ingestion. All vault and transcript queries
   go through `retrieval.retrieve_context`, embedding lane included.
4. **Provenance and the audit line** (§4), including owner-interaction
   provenance on the `source_log` seam.
5. **Inference marking and the 0.6 cap**, plus supersede-on-direct-evidence.
6. **Owner clarification.** Reuse the existing chat question surface, but make
   the question an informed inquiry outcome and classify the owner's response.
7. **Ring 1 adapters** — Gmail, Obsidian, and configured local-file indexes;
   all deficit-scoped, deterministic, read-limited, and read-only.
8. **Budgets, terminal outcomes, and ring logging** (§5), including the
   `self_state` rollup and routing `resolved_by_transcript` back at the capture
   pipeline. The metadata-only `reports/enrichment-audit.jsonl` seam now records
   each attempt's terminal outcome and stop ring, and `self_state` summarizes
   the last 30 days without copying acquired source text. Budget enforcement,
   owner outcome routing, and capture-pipeline deviation emission remain open.
9. **Raise the `policy_tier` clamp to 3** (§6), making the single `enrich`
   action reachable. Last commit, after everything above is green.

`lisan/tools/research.py` is not in this list: it is Ring 2's interface and is
built when Ring 2 is, after §9 is answered. It is specified in §3 now so that
the first web adapter is written against a contract rather than inventing one.

Step 1 is deliberately a complete feature. If Ring 1 never shipped, an agent
that reconciles its own entity stories against its own memory would still be
worth having.

## §8 — Definition of done

- Full suite green, both runners.
- Transcript-first Ring 0 enrichment tested end to end: current transcript,
  historical transcript, then vault search; entity updated, loop closed,
  provenance line present, and no later ring invoked after resolution.
- A clarification supplied in chat resolves an inquiry with
  `source_type: owner_interaction` and transcript provenance.
- Owner responses that decline, correct, mark unimportant, or prohibit
  research produce the correct terminal outcome and do not re-ask immediately.
- Deterministic source queries are tested without model calls, and configured
  local roots are portable across usernames and operating systems.
- Provenance asserted on every write path — a test that fails if an enrichment
  can land without saying where it came from.
- Inference cap and supersede-on-evidence tested.
- A bounded candidate result cannot cause an unbounded corpus read.
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

**A published-class source.** Ring 2 needs a provider behind
`lisan/tools/research.py` (§3). The installed `research` skill supplies the
source *discipline*, not the interface — it is instruction-only and returns
prose. Academic-paper search is not required for the first web adapter.

**Transcript indexing — RESOLVED 2026-08-16.** The owner selected the separate
enrichment-only lane. Transcripts remain out of the ordinary files table and
ordinary `embeddings.bin`; the lane maintains its own `transcript_embeddings.bin`
sidecar with stable content hashes and embedding hashes, and ranks bounded
candidate excerpts with lexical and local semantic similarity. This preserves
the raw transcript’s value for enrichment without changing what normal memory
retrieval returns.

The options considered, with what each costs:

- **Index and embed them like any other record.** Best retrieval, and the
  transcript corpus becomes reachable from *all* retrieval, not just
  enrichment — every future search starts surfacing raw conversation beside
  distilled records. That is a system-wide behaviour change smuggled in under a
  feature.
- **Index them into a separate lane** queried only by enrichment. Keeps
  ordinary retrieval unchanged; costs a second index path to maintain.
- **No index; bounded scan** of the last N days of transcript files at seek
  time. Cheapest, no schema change, no behaviour change anywhere else — and no
  semantic search, so it finds direct wording and misses paraphrase.

*Decision:* the separate lane. It gets the embedding search the owner asked
for without changing what ordinary retrieval returns, and the enrichment path
is the only caller that wants unfiltered conversation ranked beside curated
memory.

**Owner clarification policy.** The owner is a source of information and a
source of value-of-information judgments, but not an infallible oracle. Decide
which inquiry classes should ask the owner before Ring 2, which low-risk public
questions may proceed directly, and which owner boundaries permanently block
external research.
