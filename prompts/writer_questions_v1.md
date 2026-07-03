# Writer Questions v1

TIME RULE: stored memory outlives the conversation. Convert every relative time expression
("today", "yesterday", "tomorrow", "next week", "last night") into an absolute date using
TODAY from your input (e.g. "on 2026-07-02"). A record that says "today" is wrong the moment
the day ends.

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
