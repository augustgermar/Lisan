# Two-machine security remediation checklist

Lisan runs as two independent installations: a Debian 13 workstation and a
MacBook Pro. They share source through GitHub, but they do not share vaults,
databases, configs, credentials, runtime state, or audit history. Never infer
parity from a clean Git diff.

## Code-level changes

These changes travel through GitHub after the commits are pushed and each
machine pulls and reinstalls/restarts Lisan:

- Read-only Codex defaults and capability-scope metadata.
- Structural approval receipts for every consequential browser path.
- Content-trust field, propagation, and prompt fencing.
- Conservative fencing for legacy `unknown` trust.
- Durable, append-only, rotated browser action records.
- Tests and audit documentation.

The repository does not carry either machine's personal vault or ignored
`config.json`.

## Open item — MacBook inspection (2026-10-06)

The MacBook remains unverified and must stay that way until it is inspected
locally. On the Mac, first report the effective `sandbox_mode`,
`all_agents_sandbox_mode`, `drive.action_tier`, and `adjutant.enabled`; do not
assume parity with Debian. Then, if the owner confirms the intended posture,
set both Codex sandbox settings to `read-only`, set `drive.action_tier` to `0`,
and preserve the chosen Adjutant setting. Restart the local services, run the
health check, and verify the durable platform-resolved audit directory.

The Mac's vault-specific `content_trust` backfill, unknown-provenance records,
and pending embeddings are separate open work and are not covered by Debian's
counts in this pass.

## Required on each machine

Run separately on the MacBook and Debian workstation:

1. Pull the approved `main` commit and update the local Lisan virtualenv.
2. Inspect the machine's own `config.json`; set both
   `providers.codex.sandbox_mode` and
   `providers.codex.all_agents_sandbox_mode` to `read-only`.
3. Set `drive.action_tier` to `0` unless the owner deliberately raises it.
4. Keep `adjutant.enabled` at the chosen local posture; tier 0 does not itself
   disable Adjutant, but Adjutant still requires local intent delegations and
   capability/path allowlists.
5. Confirm the local vault and database paths; do not compare record counts
   across machines as if they were one corpus.
6. Restart local Lisan services and verify the effective settings and health
   report on that machine.
7. Confirm the local audit directory is durable and outside the repo, vault,
   and model-writable paths.

## Audit locations

Approval receipt files remain short-lived runtime state. Durable audit records
resolve by platform:

- Linux: `$XDG_STATE_HOME/lisan/audit`, or
  `~/.local/state/lisan/audit` when `XDG_STATE_HOME` is unset.
- macOS: `~/Library/Application Support/Lisan/audit`.
- `LISAN_AUDIT_DIR` may override the location, but the implementation rejects
  paths inside the repository or vault.

The directory is created mode `700`; ledger and rotated segments are mode
`600`. The action ledger is append-only, fsynced, and rotated with retained
segments.

## Scheduled current-brief regeneration

The repository provides platform-specific daily schedulers at 03:15 local:

- Linux: install `deploy/systemd/user/lisan-current-brief.service` and
  `lisan-current-brief.timer` under `~/.config/systemd/user/`, then run
  `systemctl --user daemon-reload` and
  `systemctl --user enable --now lisan-current-brief.timer`.
- macOS: copy
  `deploy/launchd/com.lisan.current-brief.plist.template` to
  `~/Library/LaunchAgents/com.lisan.current-brief.plist`, replace
  `__LISAN_HOME__` and `__LISAN_VAULT__` with that Mac install's actual paths,
  then run `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lisan.current-brief.plist`.

Both schedulers call the narrow `lisan brief` command rather than `lisan sync`,
so daily freshness does not rebuild the full index.

## Current verified scope

This remediation pass was performed on the Debian host only. The MacBook
installation remains unverified until its own checkout, config, vault, health,
and audit path are inspected directly.

The capability inventory cache is intentionally short-lived (60 seconds) and
refreshes when the repository commit changes. Historical Debian logs do not
contain the two reported false “checkin unavailable” incidents or process-
lifetime evidence, so their original root cause remains unverified.

## MacBook inspection — BEFORE security remediation (2026-10-07)

This record preserves the live local state observed before any configuration
change in this session. These are MacBook-local values; they are not inferred
from the Debian installation.

- `providers.codex.sandbox_mode`: absent; the code hard-default resolves this
  to `read-only` for the `codex` agent.
- `providers.codex.all_agents_sandbox_mode`: `danger-full-access`.
- `drive.action_tier`: `4`.
- `adjutant.enabled`: `true`.
- Effective resolved Codex sandbox: `read-only`.
- Effective resolved writer sandbox if routed through Codex: `danger-full-access`.
  The current writer routing is local-state `claude` at low/medium/high, so
  this is the Codex fallback posture rather than the currently selected writer
  provider.
- `lisan health` posture line: `!!! EFFECTIVE LIVE POSTURE: sandbox_mode=read-only | action_tier=4 | adjutant_enabled=true !!!`

The health report also recorded `all-agents sandbox setting:
danger-full-access`. The repository code and health report are CODE-derived;
the values above and the writer routing are LOCAL STATE.

## MacBook Adjutant review and remediation (2026-10-07)

Before changing the setting, the MacBook-local Adjutant history was inspected
from `repo/lisan.sqlite` and the Adjutant service logs:

- 3,347 cycles from `2026-07-23T19:01:50Z` through
  `2026-10-07T15:36:55Z`.
- 1,119 calibration dry-run cycles, 2 additional dry-run cycles, and 2,226
  non-dry-run cycles.
- 2,681 `report_only` decisions, 1,502 denied decisions, and 1,324
  `approval_overridden` records.
- No `executed`, `execute`, or `success` action records were present.
- At inspection: no pending confirmations and no approved tasks awaiting
  execution.

The available history shows intent polling and decision logging, not a
genuinely executed Adjutant action. However, this MacBook installation has no
retrospective durable action ledger for the period when it ran with
`action_tier=4` and `danger-full-access`. Unsandboxed browser/action activity
for that period is therefore **unknown, not clean**.

For this exposed installation, the remediation choice is to disable Adjutant:
`adjutant.enabled=false`. The local config now also sets both Codex sandbox
settings to `read-only` and `drive.action_tier=0`. These are LOCAL STATE
changes; the structural controls are CODE.

## Steps 3-6 local verification (2026-10-07)

The macOS `com.lisan.current-brief` LaunchAgent was rendered from
`deploy/launchd/com.lisan.current-brief.plist.template` at
`~/Library/LaunchAgents/com.lisan.current-brief.plist`, loaded successfully,
and manually kicked once. Launchd reported exit code 0 and the command wrote
`vault/primer/current-brief.md`.

Vault validation reported 0 errors and 53 warnings. The index contains 3,288
file records, 212 claims, 515 aliases, and 410 epochs. Warnings are existing
reference/alias issues, stale state files, and missing wikilinks; no validator
error was introduced by this remediation.

Embedding health after the drain: mode `auto`, provider `fastembed`, reachable;
model `BAAI/bge-small-en-v1.5`, dimension 384, 3,289 vectors; 3,288 records
embedded and 0 pending. The current brief is dated 2026-10-07, observed at
2026-10-07T15:55:18Z, valid until 2026-10-08T15:55:18Z, status current.

`content_trust` is not yet populated: the live distribution is 3,288 NULL and
zero explicitly labelled values. A dry-run proposal, not executed, is:

- 1,648 `untrusted`: explicit imported/external lineage (`source_type`, URL,
  origin, source path/document, or artifact reference).
- 46 trusted candidates: direct captured channels (`chat`, `checkin`,
  `sms_export`, `calendar`, or `manual_note`). These remain candidates until
  the write-boundary provenance is confirmed.
- 1,594 `unknown`: no reliable provenance discriminator.

The Obsidian calibration ingest is identifiable as one record,
`knowledge/frameworks/obsidian-obsidian-chunk-1.md`, with
`source_document=obsidian`; it must be classified `untrusted`, never trusted.
The other eight records mentioning Obsidian are discussion/decision/entity
records without ingest provenance and belong in `unknown`. No backfill was
performed.

The historical identity audit found nine reversible archive stubs with an
explicit `merged_into` target. Their source logs remain on the surviving
entities. The audit did not find owner distinctions or structured birthday
conflicts for those pairs, but absence of a conflict is not proof of identity;
no merge was repaired in this remediation.

A tenth historical merge marker appears in surviving source logs and prior
maintenance reports, but its archive stub is absent. This is an undocumented,
reversible-history gap and should be treated as suspicious until the original
source record and owner basis are recovered. No merge was repaired.
