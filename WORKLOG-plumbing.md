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
