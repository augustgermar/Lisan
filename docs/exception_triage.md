# Broad exception handler triage — WO-0, 2026-07-04

Scope: all 171 `except Exception` sites in `lisan/` (there are zero bare
`except:` blocks). Method: full inventory with handler bodies, bulk
classification by pattern, eyes-on inspection of every site in a
security- or quality-load-bearing path.

## Classification

### (a) Legitimate log-and-degrade — no change (~75 sites)

The handler already logs via `log_error`, surfaces the error to the user
or caller (error string, report entry, job-failure mark), or implements a
documented policy. Representative sites:

- `telegram_bot.py` loop guards (one bad update must not kill the bot;
  poll errors log and back off), `jobs.py` job-failure marking,
  `scheduler.py` tick guard, `providers/base.py` retry/transient logic,
  `providers/embeddings.py` unreachable-policy (documented), `rotato.py`
  (wraps into `ProviderError`), `capture.py:56` (logs then re-raises),
  `capture.py:240` (documented: a failed embed must never fail a
  capture), `record_fanout.py` per-fanout logging, `chat.py` init/rebuild
  failures (printed), `execution_tools.py` (error strings returned to the
  agent), `turn_router.py:92` (deterministic fallback by design),
  `memory_pipeline.py:555` (honest "failure is logged" reply),
  `plans.py:267` (documented best-effort delivery), `health_report.py`
  (logs and reports), `provider_diagnostics.py` (collects errors into the
  diagnostic result), `backup.py:105` (failure recorded in restore
  status), `firewall.py:54` (guards only the *logging* of an
  already-blocked injection — sanitization has already happened),
  `agents/base.py:163` (tool error surfaced into the tool log),
  `agents/conversation.py:71` (documented failure-naming path).

### (b) Scan-loop skip — acceptable, ledgered for a shared helper (~55 sites)

The pattern `except Exception: continue` while iterating vault records
(frontmatter parse of one file fails; the loop moves on). Affects
`dreamer_ops`, `entity_resolution`, `batch_review`, `primer_audit`,
`retrieval_graph`, `ingest`, `ingest_batches`, `confidence_decay`,
`current_brief`, `epistemic`, `narrative_state`, `skill_loader`,
`analyst_ops`, `jobs`, `record_fanout`, `cli.py` review loops.

Risk: a *corrupt* record silently vanishes from every report and from
entity resolution. Acceptable per-record, but there is no aggregate
signal. **Ledgered improvement (not WO-0):** a shared
`iter_vault_documents()` helper that yields parsed docs and logs one
aggregate warning per pass ("N records unreadable") — single seam,
~55 call sites collapse onto it. Candidate for a later maintenance pass.

### (c) Silent degrade in a load-bearing path — FIXED in WO-0 (5 sites)

The fastembed lesson: silent failure in a scoring or context path is a
frame-drop generator. Fixed:

1. `heuristic_gate.py` entity lookup → returned 0 on DB failure, silently
   zeroing the +3/+6 entity signal in the capture gate (memories about
   known people would silently skip capture). Now logs.
2. `heuristic_gate.py` high-stakes terms → a YAML parse error silently
   disabled the +4 high-stakes signal — the user's most important topics.
   Now logs.
3. `primer_index.py` known-names → an unreadable `identity.md` silently
   emptied the known-name set (entity scoring + deixis grounding). Now
   logs.
4. `conversation.py` owner profile (identity body + identity-core roster)
   → a load failure silently removed who-is-who context from every
   conversation turn — the exact precondition for the principal-confusion
   bug class fixed in earlier eval cycles. Both blocks now log.
5. `validator.py` alias-ambiguity audit → a real index failure (the
   missing-DB case was already guarded) silently skipped the spec §7.8
   duplicate-alias check. Now emits a report warning.

### Ledgered, not changed (judgment calls left visible)

- `telegram_bot.py:563` — corrupt `config.json` silently treated as no
  config; the bot then reports "not configured", which is misleading.
  Cheap UX fix, but touches the config-fallback contract; ledgered.
- `ingest.py` `_classify_sensitivity` preview read failure → sensitivity
  classification proceeds on path/type only (may under-classify a file
  whose name is innocent). Name-based exclusion upstream still applies;
  conservative-on-failure would be a behavior change; ledgered.
- `entity_story.py` draft/transcript tail readers return `""` on failure
  → a story rewrite can silently lose context (the no-shrink guardrail
  bounds the damage). Log plumbing needs a vault handle these helpers
  don't have; ledgered.

Also fixed under WO-0 (found via the failing boundary test, tracked in
its own commit): `codex_workspace()` widening to `/` when repo and vault
are disjoint trees.
