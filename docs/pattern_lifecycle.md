# Pattern Lifecycle

Lisan treats patterns as hypotheses, not identity facts.

## Flow

1. Analyst proposes a pattern from longitudinal records.
2. Skeptic reviews the pattern against external evidence and counterexamples.
3. Dreamer may only consume the pattern after governance checks pass.

## Lifecycle Statuses

- `candidate`: proposed but not yet reviewed.
- `active_hypothesis`: a plausible pattern with some support.
- `skeptic_reviewed`: Skeptic reviewed the pattern and did not reject it.
- `supported`: the pattern has enough support to be considered stable.
- `integrated`: Dreamer or another downstream process has already used it.
- `disputed`: the pattern is contested or unstable.
- `stale`: the pattern has not been refreshed recently.
- `rejected`: the hypothesis failed review.
- `retired`: the hypothesis is no longer useful.

Legacy values such as `active`, `confirmed`, and `superseded` are still accepted for backward compatibility, but new records should use the new lifecycle statuses.

## Dreamer Gate

Dreamer may only consume a pattern when all of the following are true:

- status is `skeptic_reviewed` or `supported`
- the linked Skeptic review sets `approved_for_dreamer: true`
- the pattern has at least 3 supporting records unless `strength_override: true`
- `counterexample_search.performed` is `true`
- at least one alternative explanation exists
- confidence is at least `0.65`
- there is no unresolved high-severity contradiction linked to the pattern
- the pattern is not `stale`, `rejected`, `retired`, or `disputed`
- the pattern meets the minimum age requirement unless `integration_override` is explicitly enabled

Minimum age defaults:

- `7` days for normal patterns
- `30` days for `identity_claim` and `psychological_hypothesis` patterns

Manual age override is only allowed when:

- `integration_override.enabled: true`
- `integration_override.reason` is present
- `integration_override.approved_by: user`

## Why Counterexamples Matter

Each pattern must include a counterexample search result. The search result can show that no explicit counterexamples were found, but the search itself must be recorded so the pattern remains falsifiable.

## Diagnostic Language

Patterns must not use diagnostic or pathologizing language. Motive claims and psychological claims should remain hypotheses unless strong external evidence supports them. If the wording drifts into diagnosis, validation should fail or Skeptic should reject the pattern.

## Practical Rule

If a pattern is too broad, restates a single episode, lacks supporting records, or duplicates an existing active hypothesis, the Analyst should omit it instead of inflating the pattern set.
