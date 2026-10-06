# Retrospective audit — 2026-10-06

Scope: historical irreversible browser actions executed under the former
model-supplied `owner_approved` flag.

## Findings

- The vault/log corpus contains zero historical occurrences of the literal
  `owner_approved` flag. The only matches in the repository are the new
  regression tests that prove self-assertion is refused.
- Eight of 528 turn-trace JSON files mention browser or Playwright activity,
  spanning `2026-09-22T22:03:27Z` through `2026-10-01T18:31:21Z`.
- Those traces contain user text, high-level step names, retrieval counts, and
  LLM call metadata. They do not contain browser tool arguments, browser tool
  results, click targets, navigation URLs, approval receipts, or owner-confirmation
  events.
- The live database has no browser-action audit table or receipt history. At
  audit time: `adjutant_log=0`, `confirmations=0`, `task_runs=0`,
  `turn_traces=528`, `retrieval_log=686`, and `llm_call_log=2512`.

## Conclusion

The available evidence shows no recorded use of `owner_approved`, but logging
fidelity is insufficient to answer whether an irreversible browser action was
executed without real owner confirmation. This is therefore **not** a clean
historical bill of health; it is an unprovable interval. The new receipt audit
events provide the missing approval-basis record for actions executed after
Step 2.

No historical records were retroactively edited as part of this investigation.
