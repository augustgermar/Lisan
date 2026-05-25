# Evidence and Claims

This project separates three different layers of memory:

- `evidence` records what was directly observed from an external artifact or explicit event.
- `claim` records an interpretation, hypothesis, prediction, or other assertion that may or may not be supported.
- `skeptical_review` records the Skeptic's evaluation of a draft or claim record.

The goal is to keep the memory store anchored to artifacts instead of letting narrative framing harden into fact.

## Observation vs Interpretation vs Hypothesis

### Observation

An observation is directly visible in an artifact.

Example:

- "Steve asked Alex to present the rollout plan to management."
- "The ticket was assigned on 2026-05-25."

These belong in `evidence.observed_facts`.

### Interpretation

An interpretation is a reading of what an observation means.

Example:

- "Steve tried to scapegoat Alex."
- "Leadership trusts Alex with strategy."

These belong in `claim` records, not in `evidence`.

### Hypothesis

A hypothesis is a tentative explanation that remains open to challenge.

Example:

- "Steve may have been delegating work."
- "The meeting may have been routine coordination."

These should be stored as claims with a suitable `claim_class`, usually `motive_hypothesis` or `psychological_hypothesis` when the statement concerns intent or inner state.

## Record Shapes

### Evidence

```json
{
  "type": "evidence",
  "id": "evidence.rollout-email",
  "created": "2026-05-25",
  "created_at": "2026-05-25",
  "source_type": "email",
  "source_uri": "mail://thread/123",
  "artifact_hash": "sha256:...",
  "timestamp_of_artifact": "2026-05-25T09:10:00-07:00",
  "actors": ["Steve", "Alex"],
  "arena": "work",
  "compartments": [],
  "sensitivity": "low",
  "reliability": "high",
  "summary": "Steve asked Alex to present the rollout plan to management.",
  "observed_facts": [
    "Steve asked Alex to present the rollout plan.",
    "The message referenced management."
  ],
  "verbatim_excerpt": "Please present the rollout plan to management tomorrow.",
  "linked_claims": ["claim.scapegoat-risk"],
  "linked_episodes": ["episode.2026-05-25.rollout"]
}
```

### Claim

```json
{
  "type": "claim",
  "id": "claim.scapegoat-risk",
  "created": "2026-05-25",
  "created_at": "2026-05-25",
  "claim_text": "Steve tried to scapegoat Alex.",
  "claim_class": "motive_hypothesis",
  "owner": "user",
  "status": "disputed",
  "confidence": 0.3,
  "supporting_evidence": ["evidence.rollout-email"],
  "contradicting_evidence": [],
  "linked_patterns": ["scapegoat"],
  "first_seen": "2026-05-25",
  "last_reviewed": "2026-05-25",
  "review_notes": "Interpretation remains unresolved."
}
```

## How the Skeptic Uses Evidence

The Skeptic should:

1. Identify the directly observed facts.
2. Separate those facts from the user's interpretation.
3. Check whether the claim is actually supported by the evidence.
4. Offer alternative explanations when the evidence does not force one conclusion.
5. Lower confidence when the claim overreaches the artifact.
6. Emit a `skeptical_review` record so the reasoning is auditable later.

The Skeptic should not become reflexively contrarian. A claim can be confirmed, disputed, or left active depending on the evidence.

## Migration Notes

- Existing records continue to load through the compatibility shims.
- `arena` remains accepted as a legacy alias in older records and some CLI paths.
- New records should prefer `domain` for public-facing life-domain language, while `arena` continues to mean the internal routing and privacy boundary when needed.
- `evidence` and `claim` records can coexist with older episode, decision, and state records.
- If a draft references an external artifact, create an evidence record first or alongside the claim so the interpretation stays anchored.

## Practical Rule

If you can point to the artifact and say "this is what it literally shows", that belongs in evidence.

If you need to say "this probably means", that belongs in a claim.
