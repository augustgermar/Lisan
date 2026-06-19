# Eval Harness Procedure

This is the Erasmus-side procedure for running a retrieval / operating-style
eval against the dev sandbox. It is intentionally separate from product code.

## Persona seeding

Never seed only `primer/identity.md`. Every eval persona must write:

- `primer/identity.md`
- `primer/identity-core.md`

Use the eval helper so the seeded primer stays aligned with onboarding's live
schema:

```python
from pathlib import Path

from lisan.tools.eval_seed import seed_eval_primer

seed_eval_primer(
    Path("/path/to/lisan-vault"),
    principal_name="Marcus Delgado",
    background="Senior network administrator at Halverson Networks.",
    values="Stability, explicit change control, and auditability.",
    relationships="Lucia leads compliance; Renata works closely with Marcus.",
    principal_aliases=["Marcus"],
    roster_entries=[
        {"name": "Lucia", "kind": "person"},
        {"name": "Renata", "kind": "person"},
        {"name": "Greg", "kind": "person"},
        {"name": "Devin", "kind": "person"},
        {"name": "Halverson Networks", "kind": "organization"},
        {"name": "Project Northgate", "kind": "project"},
        {"name": "Bastion", "kind": "system"},
        {"name": "Aurora", "kind": "system"},
    ],
)
```

Required verification after seeding:

- `principal_display_name(vault)` returns `Marcus`
- display rendering turns `{{principal}}` into `Marcus`, not `the user`
- roster-backed names resolve via `roster_kind(...)`

Runs without `identity-core.md` are invalid for deixis/entity evaluation.

## Preflight

Before the real run:

1. Seed the persona and clear/reset the eval vault.
2. Run one warmup capture so indexing and embeddings have work to process.
3. Assert the eval interpreter can import `fastembed`.
4. Assert semantic retrieval is live by checking `retrieval_log.vector_candidate_count > 0` after the warmup recall.
5. Abort the run if either check fails. Do not label a keyword-only run as semantic retrieval.

Example checks:

```bash
~/.lisan/venv/bin/python -c "import fastembed"
sqlite3 lisan.sqlite "select max(vector_candidate_count) from retrieval_log;"
```

## Report header

Every eval report header must record:

- exact commit hash under test
- whether the changes were committed or uncommitted working-tree edits
- provider/model per agent role
- whether embeddings were confirmed live
- timeout ceiling used for the run
- any turns that hit the timeout ceiling

## Report layout

Split the report into two sections.

### Mechanical checks

Objective pass/fail only:

- decision/state/open-loop artifacts created where expected
- drafts end in the expected terminal state
- index queue drained as expected
- `vector_candidate_count > 0`
- zero token leaks to the user surface

### Judgment findings

Model-graded or human-graded assessments only:

- fluency / tone
- whether the correction landed cleanly
- operating-style adherence
- recall answer quality beyond the mechanical grounding checks

Do not mix subjective misses into the mechanical pass/fail block.

## Operating-style probes

Record these as explicit judgment checks on every run:

- any banned opener such as `That sounds like...` or `It sounds like...`
- any response that names the user's emotion when the operating style forbids it
- any turn where the user asked for an artifact and the system deflected instead of producing it

## Timeout note

Record the per-turn timeout ceiling in the report and list any timed-out turns.
This keeps latency regressions visible and prevents timeout-split transcripts
from being mistaken for data bugs.

## Context-window note

The context-window report still monkeypatches `LisanLLM.complete`. Until the
ContextVar sink lands, generate that report last and verify the monkeypatch is
removed afterward. Treat this as a harness caveat, not a product regression.
