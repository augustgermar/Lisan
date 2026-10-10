# Graph Retrieval

Lisan retrieval is now a two-stage process:

1. Direct retrieval scores records against the query.
2. Bounded graph expansion adds a small number of linked records when they are explicitly connected and still safe to load.

The goal is to recover useful context that is only reachable through links, while keeping the system deterministic and compartment-safe.

## Ranking signals

RRF remains the primary candidate-fusion method. A bounded post-fusion adjustment
then applies durable significance, record age, recent retrieval frequency, and
query intent. High/medium/low significance is a gentle multiplier, never a
visibility gate. Age decays toward (but never to) a floor; records that appear
often in the recent retrieval log are gently diversified rather than hidden.
The access counts come from the rebuildable SQLite retrieval log, not from the
Markdown source of truth.

Typed intent is deliberately narrow: decision questions favor decision records;
explicitly historical questions favor episodes/evidence/claims; and explicit
current-state questions favor state/entity/current-claim records. Contradicted
and settled records are demoted for current queries but remain eligible, and
historical queries can still retrieve them. Relevant unresolved contradiction
notes are separately surfaced in the assembled context so ranking never
silently chooses which side is true. Disputed claims receive the same gentle
current-query demotion as explicitly contradicted records.

Entity-story compaction assigns significance to the accumulated story using the
rubric in `prompts/writer_entity_story_v1.md`. The writer must state a rationale;
if it omits or emits an invalid level, the existing level is retained.

## What Expands

Graph traversal follows explicit links only. The current expansion rules are:

- `evidence -> claim`
- `claim -> evidence`
- `claim -> contradiction`
- `claim -> pattern`
- `pattern -> supporting_records`
- `pattern -> counterexamples`
- `episode -> entity`
- `entity -> linked episodes / claims / open_loops`
- `decision -> open_loop`
- `open_loop -> decision`

The traversal is bounded to two hops.

## Safety Rules

Expansion is blocked when the target record is not visible under the active compartment rules.

Sealed or compartment-blocked records never enter the assembled context, even if they are linked from a visible record.

Cross-domain expansion is also controlled. It is allowed only when at least one of the following is true:

- the query explicitly references multiple arenas
- a linked pattern spans multiple arenas
- a Dreamer summary marks the relevant areas as coupled

Even when cross-domain expansion is allowed, it is capped at a small default budget so the context does not drift away from the query.

Defaults:

- `max_hops = 2`
- `max_expanded_records = 5`
- `max_cross_domain_records = 2`

## Output Metadata

Expanded records are rendered with explicit audit metadata:

- `expansion_source`
- `expansion_path`
- `expansion_reason`
- `hop`

The assembled context also separates direct matches from graph-expanded matches at the top of the output.

Blocked graph attempts are listed in a dedicated section so the reason is visible in plain text.

## Reading the Output

Use these cues when reviewing retrieval results:

- Direct matches are the records that scored directly against the query.
- Graph-expanded matches are only there because they were explicitly linked from a direct or already-expanded record.
- A graph-expanded record still has to pass compartment checks.
- A blocked record may exist in the index, but it was not allowed into the final context.

## Why This Matters

This layer helps Lisan recover relevant evidence, claims, patterns, decisions, and open loops that are one or two steps away from the query.

It also keeps the system from silently pulling in unsafe or off-topic records just because they are connected somewhere in the vault.
