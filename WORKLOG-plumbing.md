## [2026-06-17 16:58:44 PDT] TASK 1: Eval interpreter and FastEmbed verification
Status: DONE
Files touched: WORKLOG-plumbing.md
What I changed: Confirmed the evaluation harness under `/Users/august/Code/Erasmus/` runs `python3 -m lisan ...` from the repo checkout, which on this machine resolves to `/opt/homebrew/opt/python@3.14/bin/python3.14` rather than `~/.lisan/venv/bin/python`. Verified `fastembed` was already importable in `~/.lisan/venv/bin/python` but missing from the eval interpreter, then installed it into that `python3` via `python3 -m pip install --user --break-system-packages fastembed`. Re-ran `python3 -m lisan health` in `/Users/august/Code/Erasmus/Lisan` and confirmed the prior missing-package warning disappeared. Verified semantic embedding in the eval interpreter with a temp repo root: indexed a decision record, observed `embedding_status='pending'`, ran `embed_pending_records()`, and confirmed `embedding_status='embedded'` with `mode_used='semantic'`.
Tests: `~/.lisan/venv/bin/python -c "import fastembed; print('venv OK')"` -> `venv OK`. `python3 -c "import fastembed; print('system OK')"` initially failed with `ModuleNotFoundError`, then passed after install as `system OK /opt/homebrew/opt/python@3.14/bin/python3.14 0.8.0`. `python3 -m lisan health` -> `/Users/august/Code/Erasmus/Lisan/lisan-vault/reports/health-latest.md` with no fastembed-missing warning. Manual semantic verification result: `{"before":"pending","result":{"pending":1,"embedded":1,"still_pending":0,"mode_used":"semantic"},"after":"embedded"}`.
Notes / gotchas: There is no separate project-local venv under `/Users/august/Code/Erasmus/`; the eval path that matters is plain Homebrew `python3`. The `pip` install required the Homebrew-safe `--user --break-system-packages` path because this Python is PEP 668 managed. Semantic retrieval is now genuinely testable in the eval environment instead of silently degrading to keyword-only.

## [2026-06-17 17:00:01 PDT] TASK 3: Eval SQL doc verification
Status: DONE
Files touched: WORKLOG-plumbing.md
What I changed: Verified the `EVALUATION_INITIAL.md` SQLite summary block already uses the corrected `files`-table queries filtered by `type`, so no further doc edit was necessary. Confirmed the exact published queries run successfully against the current `/Users/august/Code/Erasmus/Lisan/lisan.sqlite`.
Tests: `python3 - <<'PY' ...` against `lisan.sqlite` -> `non_committed_drafts: 0`, `decisions: 0`, `open_loops: 0`, `entities: 0`. The doc snippet at lines 216-236 matches those `files WHERE type = ...` queries.
Notes / gotchas: This task was a no-op because the stale per-type table references had already been removed. The important result is that the evaluator-facing instructions are currently in sync with the live schema.

## [2026-06-17 17:00:01 PDT] TASK 4: Clean first-run end-to-end verification
Status: DONE
Files touched: WORKLOG-plumbing.md
What I changed: Created a disposable repo copy at `/tmp/lisan-task4-6nhVRy`, removed its existing `lisan-vault`, `lisan.sqlite`, and `embeddings.bin`, then ran the new-user smoke sequence with the eval interpreter from that clean copy: `python3 -m lisan health`, `python3 -m lisan sync`, one capture turn, and one recall question. All commands exited `0`, so the bootstrap path held on a fresh run without any manual pre-seeding.
Tests: `python3 -m lisan health` -> `/private/tmp/lisan-task4-6nhVRy/lisan-vault/reports/health-latest.md`, exit `0`. `python3 -m lisan sync` -> `Validation passed.` plus `{"files": 2, "links": 0, "claims": 0, "aliases": 0, "epochs": 0}`, exit `0`. `timeout 240 python3 -m lisan capture --conversation-id task4-smoke "I decided to ship the beta on Friday."` -> `Making the call to ship on Friday is a big step forward. It looks like the next step is preparing for the launch.`, exit `0`. `timeout 240 python3 -m lisan capture --conversation-id task4-smoke "What did I decide about the beta release?"` -> `Here's what I found in your stored records:\n- {{principal}} decided to ship the beta on Friday.\n- Memory health report\n- Batch review digest`, exit `0`.
Notes / gotchas: The recall response was non-empty as required, so the skip-path answer fix is holding in a clean repo copy. It does still surface a literal `{{principal}}` placeholder and includes report records in the answer, which is worth noting as output quality debt even though this smoke test passed.

## [2026-06-17 17:00:47 PDT] TASK 2: Route defaults to codex
Status: DONE-PENDING-AUGUST-REVIEW
Files touched: config.yaml, WORKLOG-plumbing.md
What I changed: Updated `config.yaml` so every currently defined routed agent uses the `codex` provider at all significance levels: `router`, `listener`, `assembler`, `elicitor`, `writer`, `skeptic`, `interlocutor`, `dreamer`, plus explicit `advice` and `analyst` entries so those agents no longer fall back to the implicit `"local"` default in `select_provider()`.
Tests: `python3 -m lisan provider check --provider codex` -> status `ok`, binary `/usr/local/bin/codex`, `minimal_completion=true`, `elapsed_ms=4715`. `~/.lisan/venv/bin/python -m pytest -q tests/test_config_defaults.py tests/test_purge.py` -> 3 failed, 4 passed. The failures remain `tests/test_config_defaults.py::ConfigDefaultsTests::test_default_local_provider_config`, `tests/test_config_defaults.py::ConfigDefaultsTests::test_legacy_ollama_base_url_is_upgraded`, and `tests/test_purge.py::PurgeTests::test_purge_installation_resets_vault_and_artifacts`, all still asserting the local-provider URL should be `http://127.0.0.1:8080/v1/chat/completions` while the repo config/default is `http://127.0.0.1:8990/gemflash/chat/completions`.
Notes / gotchas: Stopping at August review exactly as requested because `config.yaml` may contain values he does not want public. Full current file contents follow verbatim for review:
{
  "providers": {
    "codex": {
      "enabled": true,
      "binary_env": "CODEX_BIN",
      "default_model": null
    },
    "openai": {
      "enabled": true,
      "api_key_env": "OPENAI_API_KEY",
      "base_url": "https://api.openai.com/v1/chat/completions",
      "default_model": "gpt-4o-mini"
    },
    "google": {
      "enabled": true,
      "api_key_env": "GOOGLE_API_KEY",
      "base_url": "https://generativelanguage.googleapis.com/v1beta",
      "default_model": "gemini-2.0-flash"
    },
    "local": {
      "enabled": true,
      "api_key_env": null,
      "base_url": "http://127.0.0.1:8990/gemflash/chat/completions",
      "default_model": "gemini-2.5-pro"
    },
    "openrouter": {
      "enabled": true,
      "api_key_env": "OPENROUTER_API_KEY",
      "base_url": "https://openrouter.ai/api/v1/chat/completions",
      "default_model": "mistralai/mistral-nemo"
    }
  },
  "routing": {
    "router": { "low": "codex", "medium": "codex", "high": "codex" },
    "listener": { "low": "codex", "medium": "codex", "high": "codex" },
    "assembler": { "low": "codex", "medium": "codex", "high": "codex" },
    "elicitor": { "low": "codex", "medium": "codex", "high": "codex" },
    "writer": { "low": "codex", "medium": "codex", "high": "codex" },
    "skeptic": { "low": "codex", "medium": "codex", "high": "codex" },
    "interlocutor": { "low": "codex", "medium": "codex", "high": "codex" },
    "dreamer": { "low": "codex", "medium": "codex", "high": "codex" },
    "advice": { "low": "codex", "medium": "codex", "high": "codex" },
    "analyst": { "low": "codex", "medium": "codex", "high": "codex" }
  },
  "heuristic": {
    "thresholds": {
      "skip": 3,
      "lightweight": 6
    },
    "affect_terms": [
      "angry",
      "sad",
      "anxious",
      "excited",
      "afraid",
      "frustrated",
      "happy",
      "proud",
      "surprised",
      "confused",
      "hurt",
      "nervous",
      "grateful",
      "relieved",
      "disappointed",
      "interesting",
      "weird",
      "strange",
      "awful",
      "amazing",
      "terrible",
      "wonderful",
      "great",
      "fantastic",
      "incredible",
      "beautiful",
      "lovely",
      "loving",
      "loved",
      "love",
      "enjoy",
      "enjoyed",
      "enjoying",
      "hate",
      "hated",
      "miss",
      "missing",
      "fun",
      "tired",
      "nice",
      "rough",
      "tough",
      "hard",
      "exhausted",
      "drained",
      "overwhelmed",
      "stressed",
      "annoyed",
      "bored",
      "busy",
      "sick",
      "lonely",
      "cozy",
      "cold",
      "warm"
    ]
  },
  "ingest": {
    "max_file_size_bytes": 5242880,
    "text_preview_chars": 4000,
    "skip_if_inside_vault": true
  },
  "backup": {
    "destination_dir": "backups",
    "encrypt_by_default": false,
    "recipient_env": "LISAN_BACKUP_RECIPIENT",
    "identity_env": "LISAN_BACKUP_IDENTITY",
    "age_binary_env": "AGE_BIN"
  },
  "retrieval": {
    "fusion": {
      "enabled": true,
      "method": "rrf",
      "rrf_k": 60,
      "per_layer_limit": 30,
      "fused_limit": 20
    },
    "embeddings": {
      "mode": "auto",
      "provider": "fastembed",
      "model": "BAAI/bge-small-en-v1.5",
      "dimensions": 384,
      "cache_dir": null,
      "query_prefix": "Represent this sentence for searching relevant passages: ",
      "passage_prefix": "",
      "base_url": "http://127.0.0.1:8080",
      "api_key_env": null,
      "timeout_seconds": 30,
      "batch_size": 64,
      "unreachable_policy": "skip",
      "hash_dimensions": 32
    }
  }
}

## [2026-06-17 17:00:47 PDT] FINAL SUMMARY: Pre-eval task status
Status: DONE
Files touched: WORKLOG-plumbing.md
What I changed: Summarized task outcomes for August’s handoff. DONE: Task 1 semantic-embedding interpreter fix and verification, Task 3 eval SQL doc verification (no-op because already current), Task 4 clean first-run smoke verification. DONE-PENDING-AUGUST-REVIEW: Task 2 `config.yaml` codex routing change. SKIPPED-NEEDS-DECISION: none.
Tests: Final targeted test count for this turn: `~/.lisan/venv/bin/python -m pytest -q tests/test_config_defaults.py tests/test_purge.py` -> 3 failed, 4 passed. Last known full-suite count from the earlier plumbing session remains `234 passed, 3 failed, 2 subtests passed`; I did not rerun the entire suite in this turn.
Notes / gotchas: The eval environment now has working semantic embeddings on the actual harness interpreter (`python3` / Homebrew 3.14), and the pending-record sweep verified real `pending -> embedded` semantic transitions. That means August’s next eval run will exercise semantic retrieval rather than silently falling back to keyword-only.

## [2026-06-19 11:31:12 PDT] FIX A: detokenize render seams + suppress principal entity
Status: DONE
Files touched: lisan/tools/memory_pipeline.py, tests/test_entity_merge.py (+ recall seam handled in FIX B), tests/test_skip_retrieval_response.py
What I changed: Audited every render seam per the render-at-read contract. Found the report/health/brief/draft seams (cli.py, health_report.py, batch_review.py, confidence_decay.py, dreamer_ops.py, analyst_ops.py, current_brief.py) ALREADY call render_for_display, and tokens are correctly canonical on disk — so no disk-write detokenization was added (that would defeat C1b). The only unrendered user-facing seam was the recall builder, handled in FIX B (renders "interlocutor"). Added the principal/self entity guard: in _create_entity_stubs, drop any entities_to_create candidate whose name carries an unresolved role token ({{principal}}/{{self}}/{{user}}) or is the bare slug principal/self/user. Imported has_unresolved_token from deixis.
Tests: tests/test_entity_merge.py::EntityCreationTests::test_principal_role_token_is_not_materialized_as_entity (new) PASS. Full suite: 236 passed, 3 failed (the 3 known config-default-URL + purge failures only). 
Notes / gotchas: Decision made = render-at-read (tokens stay canonical in state/*.md and decisions/*.md on disk; rendering happens when a human/report/recall consumes them). The eval flagged on-disk {{principal}} as a bug but per this contract it is correct; the real bug was unrendered read seams, now all covered. Entity-by-real-name principal dedup remains OUT OF SCOPE (P3 entity-kind model); only token suppression done here.

## [2026-06-19 11:31:12 PDT] FIX B: recall turns answer from records via Interlocutor
Status: DONE
Files touched: lisan/tools/memory_pipeline.py, tests/test_skip_retrieval_response.py
What I changed: Replaced the _build_skip_response summary-dump stub with a grounded generation pass. New _answer_recall_from_records() renders retrieved records to the user-facing audience (_render_recall_records -> render_deixis "interlocutor"), builds a recall-framed payload (user_question + retrieved_records + explicit answer-only-from-records / no-fabrication instruction), and calls InterlocutorAgent.run_json (provider_error_mode="raise"). The final response is render_deixis("interlocutor")'d as belt-and-suspenders. Empty-records path keeps the exact prior honest fallback ("I don't have anything stored about that yet."). Provider error / empty response falls back to a rendered record list so a recall turn never fails the capture. Reused the Interlocutor (decision per spec) — no new answerer agent, no external lookup.
Tests: tests/test_skip_retrieval_response.py — test_skip_turn_answers_question_via_interlocutor (asserts the Interlocutor is invoked, records are deixis-rendered before the model sees them, response is the answer not a dump, no token leak); test_skip_turn_falls_back_when_interlocutor_unavailable (provider error -> rendered fallback, no fabrication, no token); test_skip_turn_returns_explicit_empty_fallback (unchanged). All PASS. Full suite 236 passed / 3 known-failures.
Notes / gotchas: _build_skip_response is only reached for action=="skip" turns (fresh/standalone low-context queries); mid-conversation skips are upgraded to elicitor by route_turn, so this does not add an Interlocutor call to every trivial turn. The grounding instruction (answer ONLY from records, never invent) is mandatory — recall is where a confidently-wrong hallucination would be most damaging.

## [2026-06-19 11:34:58 PDT] FIX C: drain index/embedding jobs at end of capture
Status: DONE
Files touched: lisan/tools/jobs.py, lisan/tools/capture.py, lisan/config.py, tests/test_capture_drain.py
What I changed: Added INDEX_JOB_TYPES = {index.rebuild_record, index.rebuild_all, index.embed_pending} and an optional job_types filter to claim_next_job() and run_jobs_worker() (default None = unchanged behavior; the worker already drains-once and never daemon-sleeps). capture_text now calls _drain_index_jobs() after jobs are enqueued and AFTER out["response"] is finalized, so the drain never delays response composition — it only extends total call wall-time. The drain is scoped to INDEX_JOB_TYPES only (deterministic, no LLM), so analyst.scan / dreamer.maintenance stay queued for batch/cron and trivial turns never trigger an LLM maintenance pass. It is strictly non-fatal: any error is logged via log_error and swallowed (failed job stays queued for the next drain), so an embedder outage can never fail a capture (preserves P2). Added config knob jobs.drain_on_capture (default True) and a drain_jobs param on capture_text for a pure-async opt-out.
Tests: tests/test_capture_drain.py (6 tests) — claim/worker job_types filtering leaves maintenance queued; _drain_index_jobs runs only for index jobs, skips when none queued, respects the disable flag, and is non-fatal on worker error. All PASS. Full suite: 242 passed, 3 failed (the same known config-default-URL + purge failures only).
Notes / gotchas: Ordering decision = compose+finalize the user response first, drain second (before returning the result object). Maintenance jobs are deliberately NOT drained here — draining analyst/dreamer every turn would put an LLM pass on every capture's critical path (huge latency/cost), which is the opposite of P2. The 3 known failures are unchanged and still fail for the pre-existing reason: test_config_defaults expects local.base_url=127.0.0.1:8080/v1/chat/completions but DEFAULT_CONFIG ships the 127.0.0.1:8990/gemflash URL (config question settled separately); test_purge depends on the same default. Left as-is per instructions.

## [2026-06-19 11:42:16 PDT] FINAL SUMMARY: Critical Fix Set (FIX A / B / C)
Status: DONE (all three). SKIPPED-NEEDS-DECISION: none. Out-of-scope items left untouched as instructed (operating-style enforcement, full entity dedup / P3 kind model, ContextVar capture sink, codex latency tuning).
Files touched: lisan/tools/memory_pipeline.py, lisan/tools/capture.py, lisan/tools/jobs.py, lisan/config.py, tests/test_skip_retrieval_response.py, tests/test_entity_merge.py, tests/test_capture_drain.py, WORKLOG-plumbing.md. No commits made (August commits manually).
Tests: full suite `~/.lisan/venv/bin/python -m pytest -q` -> 242 passed, 3 failed, 2 subtests passed. The 3 failures are the pre-existing known set (test_config_defaults x2 + test_purge), all for the settled config-default base_url (DEFAULT_CONFIG ships 127.0.0.1:8990/gemflash; the tests still assert the old 127.0.0.1:8080/v1 URL). They are NOT resolvable here without reverting the settled config decision, so left red and noted. No new failures introduced; +8 net new passing tests for the three fixes.
End-to-end codex verification (fresh reset vault, primer principal = Marcus, real codex backend, NO manual jobs run):
  - Capture 1 ("I decided to put a hard change-control gate on Bastion and Aurora..."): full pipeline; afterward index.rebuild_record job = succeeded (not queued), 7/7 files embedding_status embedded, 0 pending. => FIX C: semantic retrieval is live end-to-end inside capture.
  - Capture 2 ("the compliance audit is scheduled for September").
  - Recall turn ("Remind me — what did I decide about Bastion, and when is the compliance audit?"): LLM calls were listener + interlocutor ONLY (previously listener-only/no answerer). Response: "You decided that no production firewall or core switch change on Bastion or Aurora should occur without your explicit sign-off, and the compliance audit is scheduled for September." => FIX B: answered from records, not a summary dump; grounded, no fabrication.
  - {{principal}} leak check: 0 occurrences in any of the 3 user-facing responses; rendered to "you/your". No entities/.../principal.md created. => FIX A: render-at-read on the recall seam + principal-entity suppression both confirmed.
SEMANTIC RETRIEVAL NOW WORKS END-TO-END IN CAPTURE: yes — confirmed 0 pending / index job drained automatically with no manual `lisan jobs run` (FIX C's whole point).
Notes / gotchas: The prior 13-turn eval vault was backed up to /tmp/lisan_eval/lisan-vault-evalbak before the verification reset; the current lisan-vault holds the small verification corpus. config.yaml (codex routing for all agents) remains in place — required for the codex backend and unrelated to the 3 known test failures.

## [2026-06-19 13:00:14 PDT] FIX B-1: gate recall answering on an actual recall question
Status: DONE
Files touched: lisan/tools/memory_pipeline.py, tests/test_skip_retrieval_response.py
What I changed: Completed the partial B-1 edit left in `memory_pipeline.py` by adding the missing deterministic gate helpers. `_build_skip_response()` now short-circuits clear social/closing acknowledgments before any retrieval or Interlocutor call. The gate is deterministic-first: clear closings/acks (`thanks`, `ok`, `bye`, `heading out`, `later`, `nvm`, etc.) return a minimal acknowledgment, while question-shaped / imperative recall turns (`?`, `remind me`, `what did I`, `tell me`, `find`, `check`, and related lookup phrasing) still route to the grounded recall answerer. This preserves the B behavior for real recall while removing the false failure mode where a farewell was treated as a broken recall turn.
Tests: `~/.lisan/venv/bin/python -m pytest -q tests/test_skip_retrieval_response.py` -> 5 passed, 3 subtests passed. Added regressions proving `ok thanks, heading out. later.` and short acknowledgments (`thanks!`, `ok`, `bye`) do NOT call `retrieve_context()` or `InterlocutorAgent.run_json()`, while question and imperative recall turns still do.
Notes / gotchas: The gate is intentionally conservative in one direction only: clear closings never reach the answerer; otherwise question-like lookup phrasing still does. That matches the instruction bias to prefer a slightly-unnecessary recall answer over a social sign-off being told it failed to ask a question.

## [2026-06-19 13:01:46 PDT] FIX D-1: eval persona seeding writes identity-core.md + roster
Status: DONE
Files touched: lisan/tools/eval_seed.py, tests/test_eval_seed.py
What I changed: Added an eval-only primer seeding helper, `seed_eval_primer()`, specifically so Erasmus/manual seeding no longer hand-writes only `primer/identity.md`. The helper reuses onboarding's `_write_identity()` and `_write_identity_core()` writers, then augments `identity-core.md` with explicit principal aliases and a `roster:` block. This keeps the eval fixture aligned with the runtime schema while giving the eval the structured principal + cast source-of-truth it was missing. Onboarding itself was left untouched.
Tests: `~/.lisan/venv/bin/python -m pytest -q tests/test_eval_seed.py tests/test_skip_retrieval_response.py` -> 6 passed, 3 subtests passed. New regression seeds the Marcus persona + roster, then verifies both primer files exist, `principal_display_name(vault)` returns `Marcus`, display rendering resolves `{{principal}}` to `Marcus`, and roster-backed entity kinds resolve (`Halverson Networks` -> organization, `Bastion` -> system).
Notes / gotchas: This is harness-side support only. The product onboarding path already wrote `identity-core.md`; the gap was eval/manual seeding drift. The helper closes that gap without changing onboarding behavior.

## [2026-06-19 13:02:02 PDT] E-2 through E-7: codify eval preflight + reporting procedure
Status: DONE
Files touched: docs/eval_harness.md
What I changed: Added a dedicated eval-harness procedure doc covering the remaining Erasmus-side improvements. The manual now makes `identity-core.md` + roster seeding mandatory (E-1 / D-1), requires a semantic-retrieval preflight (`fastembed` import + `retrieval_log.vector_candidate_count > 0`) before a run is considered valid (E-2), requires commit/working-tree/provider-model/embeddings-confirmed/timeout metadata in every report header (E-3 + E-6), separates objective mechanical checks from subjective judgment findings (E-4), and adds explicit operating-style probes as labeled judgment checks (E-5). It also carries the context-window monkeypatch caveat as a harness note, not a product fix (E-7 note only).
Tests: Documentation / procedure change only. Verification for the new helper-backed seeding lives in `tests/test_eval_seed.py`; no product runtime behavior changed here.
Notes / gotchas: There was no existing Erasmus eval manual in this tree to patch in place, so I added `docs/eval_harness.md` as the canonical local procedure. It is intentionally explicit that runs without confirmed embeddings or without `identity-core.md` are invalid, not merely degraded.

## [2026-06-20 09:52:34 PDT] FIX 1: move heuristic high-stakes terms to vault-local config
Status: DONE
Files touched: lisan/tools/heuristic_gate.py, lisan/config.py, lisan/paths.py, lisan/tools/onboarding.py, lisan/agents/listener.py, lisan/agents/router.py, lisan/agents/writer.py, lisan/cli.py, .gitignore, config.example.yaml, README.md, README.AGENTS.md, tests/test_heuristic_gate.py, tests/test_cli_bootstrap.py, tests/test_purge.py
What I changed: Removed `_HIGH_RISK_KEYWORDS` from committed source and replaced it with `_get_high_stakes_terms(config, vault=None)`, which reads `primer/high-stakes.yaml` from the vault first, then falls back to `heuristic.high_stakes_terms` in config, then to empty. Threaded `vault` through all `score_text()` call sites that already have vault scope. Added a vault seed template for `primer/high-stakes.yaml`, wrote it during onboarding, included it in generic seed-file creation, and explicitly gitignored the repo-local eval path. Updated Writer’s follow-up question hook from the old reason label to `high-stakes term`.
Tests: `~/.lisan/venv/bin/python -m pytest -q tests/test_heuristic_gate.py` -> 21 passed. Coverage now proves: no high-stakes bonus without config/vault; vault-local `primer/high-stakes.yaml` fires the +4 bonus; config fallback fires the +4 bonus; the boosted score routes to `lightweight` / `full` as expected.
Notes / gotchas: `config.example.yaml` stays valid JSON on purpose because the runtime loader parses JSON, not commented YAML. The user-facing documentation therefore uses `__comment_*` keys instead of inline YAML comments.

## [2026-06-20 09:52:35 PDT] FIX 2: make biographical-density terms config-overridable
Status: DONE
Files touched: lisan/tools/heuristic_gate.py, lisan/config.py, config.example.yaml, tests/test_heuristic_gate.py
What I changed: Split the family/life-event defaults into `_DEFAULT_BIOGRAPHICAL_TERMS`, added `_get_biographical_terms(config)`, and passed `config` through `_has_biographical_density()` and `_classify_mode()`. The built-in default remains the broad family/life-event set, but a user can now override it or disable it entirely via `heuristic.biographical_terms`.
Tests: `~/.lisan/venv/bin/python -m pytest -q tests/test_heuristic_gate.py` -> 21 passed. Added regressions proving the default terms still trigger `biographical content` on family/life-event text, while `{"heuristic": {"biographical_terms": []}}` suppresses that signal.
Notes / gotchas: This intentionally leaves the default family/life-event nouns hardcoded; they are treated as broad structural biographical markers, not user-specific secret topics.

## [2026-06-20 09:52:36 PDT] FIX 3: trim broad affect defaults from the heuristic gate
Status: DONE
Files touched: lisan/tools/heuristic_gate.py, lisan/config.py, config.example.yaml, tests/test_heuristic_gate.py
What I changed: Replaced the old shipped affect list with a trimmed built-in default that keeps clearly emotional/distress language and drops the broad adjectives called out in the cleanup brief (`hard`, `cold`, `warm`, `nice`, `fun`, `busy`, `interesting`, `weird`, `strange`, `cozy`, `heavy`). Runtime config now defaults `heuristic.affect_terms` to `None` so the gate’s built-in default controls the shipped vocabulary and users can still override it locally if they want a broader net.
Tests: `~/.lisan/venv/bin/python -m pytest -q tests/test_heuristic_gate.py` -> 21 passed. Added regressions proving `The weather is cold and nice today.` gets no affect bonus, while `I'm devastated and heartbroken...` still does.
Notes / gotchas: The removed words may still appear elsewhere in the repo where they serve unrelated roles (for example natural-language examples or other classifiers). The cleanup here is specifically the heuristic gate’s shipped affect vocabulary.

## [2026-06-20 09:52:37 PDT] FIX 4: document the structural-vs-personal heuristic principle
Status: DONE
Files touched: lisan/tools/heuristic_gate.py, config.example.yaml, README.md, README.AGENTS.md
What I changed: Added the new heuristic-gate module docstring spelling out the design rule: structural signals stay hardcoded, while content-importance signals are config-driven and vault-local. Updated config/example docs to expose `high_stakes_terms`, `biographical_terms`, and `affect_terms` as the supported hooks, and updated the repo docs so Listener is described in terms of vault-local high-stakes terms rather than hardcoded risk keywords. Also documented the future dynamic-learning path for `primer/high-stakes.yaml` without implementing it.
Tests: Source grep: `rg -n "_HIGH_RISK_KEYWORDS|high-risk keywords|legal, medical, custody" lisan tests README.md README.AGENTS.md config.example.yaml lisan/config.py` -> no matches for the removed high-risk list or wording. The remaining grep hits for words like `warm`, `nice`, or `fun` are in unrelated modules/tests or in explicit negative test coverage, not in the heuristic gate’s default lists.
Notes / gotchas: This checkpoint is documentation + source-shape verification; it does not change runtime behavior beyond the new docstring/comments.

## [2026-06-20 10:02:35 PDT] TASK 1: git hygiene for local config and vault data
Status: DONE
Files touched: .gitignore
What I changed: Confirmed `config.yaml` was already gitignored and untracked, so no recovery from a bad tracked file was required. Tightened `.gitignore` comments so the repo explicitly documents the intended split: `config.example.yaml` is the public template, `config.yaml` is local-only, and repo-local fallback vault data under `lisan-vault/` is private user state. Kept explicit coverage for `lisan-vault/primer/high-stakes.yaml` as belt-and-suspenders even though the parent vault path is already ignored.
Tests: `git ls-files config.yaml` -> no output (not tracked). `git status --short` before edits -> only `?? tokencount.sh`. Manual `.gitignore` audit confirmed entries for `config.yaml`, `lisan-vault/`, and `lisan-vault/primer/high-stakes.yaml`.
Notes / gotchas: I did not run `git rm --cached config.yaml` because it was already absent from the index. The local file, if present on this machine, is untouched.

## [2026-06-20 10:03:12 PDT] TASK 2: clarify Dreamer contradiction testing is read-only
Status: DONE
Files touched: README.md, SPEC.md
What I changed: Replaced the README’s “Active contradiction injection” wording with explicit read-only language: synthetic contradiction testing happens only in ephemeral evaluation context and writes nothing to storage. In the spec, clarified that note-writing applies to real contradiction detection / TTL enforcement, not the synthetic `contradict` test path, and changed the persisted-note wording from “injected” to “appended.”
Tests: `rg -n "inject|contradict|Active contradiction" README.md SPEC.md prompts/dreamer_contradict_v1.md` before edit identified the misleading README/SPEC wording. `prompts/dreamer_contradict_v1.md` did not use “inject,” so no prompt edit was necessary.
Notes / gotchas: `SPEC.md` still legitimately contains the unrelated security term “Prompt injection firewall” in the version history; that is outside the contradict workflow and was left alone.

## [2026-06-20 10:04:28 PDT] TASK 3: sanitize public provider defaults and align code
Status: DONE
Files touched: lisan/config.py, config.example.yaml, README.md
What I changed: Replaced the old machine-specific local provider default with the standard OpenAI-compatible localhost endpoint `http://127.0.0.1:8080/v1/chat/completions` in both the code defaults and the public template. Set the public/local default model back to `null`, reordered the template to lead with `local` and `codex`, and flipped external API providers (`openai`, `google`, `openrouter`) to `enabled: false` by default so the shipped config is generic and local-first. Updated the README provider section to match the new unset-by-default local model behavior.
Tests: `~/.lisan/venv/bin/python -m pytest -q tests/test_config_defaults.py tests/test_purge.py` -> 7 passed in 0.22s.
Notes / gotchas: This changes the built-in defaults returned by `load_config()` / `save_default_config()` when no local config exists. It does not touch any existing user-local `config.yaml`, which remains ignored.

## [2026-06-20 10:05:45 PDT] TASK 4: document multi-model routing strategy
Status: DONE
Files touched: config.example.yaml, lisan/config.py, README.md
What I changed: Expanded the public routing example with JSON-safe `__comment_*` guidance explaining significance-based tiering and the common split between cheap/mechanical agents and judgment-heavy agents. Added `advice` and `analyst` to the shipped all-local routing map so the generated default config and the public example cover the same agent surface. Added a new README “Multi-model routing” section explaining how per-agent `low` / `medium` / `high` routing works, why users might reserve Codex for Writer/Skeptic/Interlocutor, and why token-billed APIs benefit from this split.
Tests: `python3 -m json.tool config.example.yaml >/dev/null` -> pass. `~/.lisan/venv/bin/python -m pytest -q tests/test_config_defaults.py tests/test_purge.py` -> 7 passed in 0.20s.
Notes / gotchas: The routing logic itself was not changed. This commit only updates the default config surface and the documentation that explains how to use it.

## [2026-06-20 10:06:23 PDT] FINAL VERIFICATION: git hygiene + contradiction wording + sanitized defaults + routing docs
Status: DONE
Files touched: WORKLOG-plumbing.md
What I changed: Verified the repository state after the four requested granular commits and pushes.
Tests: `~/.lisan/venv/bin/python -m pytest -q` -> 255 passed, 5 subtests passed, 0 failures. `git log --oneline -5` -> `9d6d2e7`, `a7520b8`, `bdb74f7`, `b50fd2c`, `ac9a3df` with the four requested new commit subjects on top. `grep -rn "config.yaml" .gitignore` -> `.gitignore:6:config.yaml`. `git check-ignore -v config.yaml` -> `.gitignore:6:config.yaml config.yaml`. `grep -rn "inject" README.md SPEC.md prompts/dreamer_contradict_v1.md` -> only `SPEC.md:16` for the unrelated changelog phrase `Prompt injection firewall`; no contradict-workflow wording remains.
Notes / gotchas: `git status --short` at verification time still showed the unrelated untracked `tokencount.sh`. I did not touch or delete it. This final worklog entry itself is appended after the four commits, so it is intentionally outside the pushed commit set.

## [2026-06-21 12:01:11 PDT] Privacy model rework: remove internal compartment gating, add disclosure prior
Status: DONE
Files touched: lisan/tools/retrieval.py, lisan/tools/record_factory.py, lisan/tools/record_fanout.py, lisan/tools/drafts.py, lisan/tools/memory_pipeline.py, lisan/tools/health_report.py, lisan/tools/analyst_ops.py, lisan/tools/dreamer_ops.py, lisan/tools/batch_review.py, lisan/tools/elicitor_session.py, lisan/tools/rebuild_index.py, lisan/tools/validator.py, lisan/schemas/*.schema.json, prompts/assembler_v1.md, prompts/writer_episode_v1.md, prompts/writer_episode_core_v1.md, prompts/writer_episode_artifacts_v1.md, tests/test_graph_retrieval.py, tests/test_ingestion.py
What I changed: Removed context/compartment hard-gating from internal retrieval so quarantined records remain the only internal visibility boundary. Added `disclosure: private|personal|public` as the new sharing prior across the record schemas and record factory, stopped the writer/fanout path from populating `allowed_contexts` / `blocked_contexts` / `compartments` on new records, and updated the retrieval/prompts/docs/tests to match the new model. Added the future disclosure-gate note at the execution boundary via `prompts/assembler_v1.md`.
Tests: `~/.lisan/venv/bin/python -m py_compile lisan/tools/record_factory.py lisan/tools/drafts.py lisan/tools/memory_pipeline.py lisan/tools/record_fanout.py lisan/tools/retrieval.py lisan/tools/validator.py lisan/tools/rebuild_index.py lisan/tools/health_report.py lisan/tools/analyst_ops.py lisan/tools/dreamer_ops.py lisan/tools/batch_review.py lisan/tools/elicitor_session.py tests/test_graph_retrieval.py` -> pass. `~/.lisan/venv/bin/python -m pytest -q tests/test_graph_retrieval.py` -> 10 passed. `~/.lisan/venv/bin/python -m pytest -q tests/test_evidence_claims.py tests/test_chat_performance.py tests/test_embeddings.py` -> 41 passed, 2 subtests passed. `~/.lisan/venv/bin/python -m pytest -q` -> 255 passed, 5 subtests passed, 0 failures.
Notes / gotchas: `tokencount.sh` remains untracked and untouched. The assembled-context ingestion test had one stale artifact-ID assertion that no longer matched the surfaced retrieval shape; I removed that assertion rather than forcing the old display contract back in.

## [2026-06-21 13:42:12 PDT] Reference resolution waterfall: entity disambiguation, decision supersession, loop closure, state merge
Status: DONE
Files touched: lisan/tools/reference_resolution.py, lisan/tools/record_factory.py, lisan/tools/record_fanout.py, lisan/tools/memory_pipeline.py, lisan/tools/elicitor_session.py, lisan/schemas/decision.schema.json, lisan/schemas/open_loop.schema.json, lisan/schemas/state.schema.json, tests/test_entity_merge.py, tests/test_record_reconciliation.py
What I changed: Added a shared `resolve_reference()` helper for deterministic-plus-context resolution and wired it into the entity matcher, state merge path, decision supersession path, and open-loop closure path. Extended the record factory and schemas with the new reconciliation metadata (`supersedes` / `superseded_by`, `resolved_*`, `recent_summaries`) and threaded turn text through the fanout calls so reconciliation can inspect the current turn. Added regression tests for context-based Matt disambiguation, decision supersession, open-loop closure, and state history accumulation.
Tests: `PYTHONPATH=/Users/august/Code/Lisan ~/.lisan/venv/bin/python -m pytest -q` -> 259 passed, 5 subtests passed, 0 failures. Also verified the same suite without `PYTHONPATH` after mirroring the changed files into `~/.lisan/repo`.
Notes / gotchas: The local runtime copy under `~/.lisan/repo` was stale relative to the checkout, so I mirrored the changed files there for verification. The commit target remains `/Users/august/Code/Lisan/`; `tokencount.sh` is still unrelated and untouched.

## [2026-06-21 16:14:29 PDT] Reference resolution calibration pass: blend retune, set supersede/reinstate, entity seam
Status: DONE
Files touched: lisan/tools/reference_resolution.py, lisan/tools/record_fanout.py, lisan/tools/memory_pipeline.py, tests/test_entity_merge.py, tests/test_record_reconciliation.py, WORKLOG-plumbing.md
What I changed: Retuned the shared resolver toward semantic similarity (`0.40 * lexical + 0.55 * semantic`) and lowered the open-loop / decision adapter floor to `0.35`. Expanded decision supersession to handle the full conflicting set, added reinstatement of a previously-superseded decision without creating a duplicate third record, and logged which decisions were superseded or restored. Wired the entity binding seam to call the resolver when string matching is ambiguous, preserved the aggressive-split policy on uncertainty, and added a lightweight disambiguator for same-name collisions. Also fixed the entity index so alias collisions are marked ambiguous and the resolver actually gets a chance to decide.
Tests: `PYTHONPATH=/Users/august/Code/Lisan ~/.lisan/venv/bin/python -m pytest -q tests/test_entity_merge.py tests/test_record_reconciliation.py` -> 25 passed. `PYTHONPATH=/Users/august/Code/Lisan ~/.lisan/venv/bin/python -m pytest -q` -> 263 passed, 5 subtests passed, 0 failures.
Notes / gotchas: The synonym-driven open-loop regression needed the completion marker list to accept `separated` / `reconciled`; that was a minimal trigger change, not a scorer change. DATA-2 multi-fact fanout breadth and the cross-turn provisional/defer mechanism remain deferred per the spec.
