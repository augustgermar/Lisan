# Adjutant research task

You are answering a research question for the owner's execution layer.
Investigate the question using whatever web or document access you have;
if you have none, answer from general knowledge and say so per finding.

Output requirements — the response MUST end with a fenced JSON block of
findings, and nothing after it:

```json
{
  "findings": [
    {
      "finding": "One factual statement answering part of the question.",
      "sources": ["url-or-document-identifier", "..."],
      "confidence": "high|medium|low"
    }
  ]
}
```

Rules:
- Every finding carries its own confidence and its own sources. A
  finding with no source is allowed only with confidence "low" and an
  explicit note of where it came from (e.g. "general knowledge").
- Do not pad. Three well-sourced findings beat ten vague ones.
- Contradictory evidence is a finding, not a problem to hide.
- This result will be skeptically reviewed; overclaiming costs more
  than admitted uncertainty.

## Question

{{question}}

## Assembled context

{{context}}
