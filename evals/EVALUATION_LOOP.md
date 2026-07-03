# The automated evaluation & self-improvement loop

North star for this round: IG-11 — self-awareness, strategy, cognition,
memory function. The evaluator simulates the owner (invented details are
fine), reviews everything the system did, and fixes code in the same cycle.

## Protocol (one cycle)

1. **Drive**: `python3 evals/driver.py --conversation <id> --text "<turn>"`
   from the dev repo — dev code runs directly against the live vault/db, so
   fixes apply on the next turn without a deploy. Fresh conversation id per
   scenario; multi-turn scenarios reuse the id.
2. **Review**: the driver's JSON (response, tools, latency) + the turn trace
   (`vault/logs/traces/`) + vault deltas (`--snapshot` / `--delta`) + job
   results (`lisan jobs list`, capture.observe payloads carry tool results).
   Verify CLAIMS against ARTIFACTS: anything the agent says it did must be
   visible in a file or a job row.
3. **Fix**: code change + regression test + entry in `evals/findings.md`,
   then commit (granular, reasoned) and push; deploy to prod
   (`git pull` in ~/.lisan/repo + `launchctl kickstart -k gui/$UID/com.lisan.telegram`)
   at least at end of session.
4. **Retest** the scenario that failed before moving on.

## Hard constraints

- NEVER modify files outside the Lisan install (repo, vault, database).
  The owner's Obsidian vault and personal documents are read-only sources.
  (Enforced in code: executor sandbox + workspace + briefing — but verify
  after any run_codex-driven scenario.)
- The vault is disposable this round; conversations may invent facts about
  the owner's world.
- The full test suite stays green through every commit.

## Scenario axes (rotate; extend freely)

memory precision (kinship shorthand, cross-entity recall, temporal
staleness) · thread continuity (deference, interruptions, topic switches) ·
action + approval (ingest requests, denials explained honestly) ·
self-awareness (state, capabilities, not-built honesty) · corrections
(target record actually updated) · scheduling & plans (created, executed,
reported) · strategy under ambiguity ("something feels off — fix it") ·
voice (humor register, no internal mechanics leakage).

## Current status

See `evals/findings.md`. Cycle 1-2 complete: owner-profile injection,
executor write boundary (the Obsidian violation — restored + triple-fixed),
messaging honesty, correction fan-out verified to hit the entity record.
