# Self Model Seed Evaluation

This evaluation corpus is a small synthetic vault that exercises the evidence, claim, pattern, Skeptic, and Dreamer lifecycle.

## How To Run

From the repository root:

```bash
python3 -m lisan evaluate seed --vault /path/to/vault
```

For test runs, copy `tests/fixtures/self_model_seed/vault` into a temporary vault and point the command at that copy.

## What The Output Shows

The evaluation report includes:

- generated pattern IDs
- hypothesis text
- supporting records
- counterexamples
- confidence
- Skeptic approval status
- Dreamer eligibility
- the blocked reason when a pattern is not eligible

## What Good Output Looks Like

Good output usually has:

- at least one pattern generated from repeated evidence
- hostile motive claims staying disputed
- overgeneralized identity claims being downgraded
- counterexamples appearing in the pattern review
- patterns blocked from Dreamer when they are too new or do not meet governance requirements

## Failure Modes To Watch For

- user interpretations being promoted to fact
- diagnostic or pathologizing language slipping into a pattern hypothesis
- patterns becoming Dreamer-eligible without counterexample search or sufficient support
- a broad pattern being treated as a clean explanation for different causal situations

## Interpreting Blocked Patterns

A blocked pattern is not necessarily wrong. It may simply be:

- too new
- too sparse
- missing counterexample search
- too broad
- underreviewed by Skeptic
- lacking enough support for Dreamer integration

Blocked patterns are useful because they show where the memory system is still being conservative.
