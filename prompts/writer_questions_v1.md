# Writer Questions v1

Generate clarifying questions ranked by consequence.

Priority order:
1. Identity confusion
2. Date/time ambiguity
3. Legal/financial/medical claims
4. Causal claims
5. Emotional interpretation
6. Minor detail

Return JSON with:
- `record_type`
- `summary`
- `significance`
- `frontmatter`
- `sections`
- `questions`
- `significance_rationale`
