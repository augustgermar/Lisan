# Graph Retrieval

Lisan retrieval is now a two-stage process:

1. Direct retrieval scores records against the query.
2. Bounded graph expansion adds a small number of linked records when they are explicitly connected and still safe to load.

The goal is to recover useful context that is only reachable through links, while keeping the system deterministic and compartment-safe.

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
- a Dreamer summary marks the domains as coupled

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
