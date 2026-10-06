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

This remediation pass was performed on Debian host `august` only. The MacBook
installation remains unverified until its own checkout, config, vault, health,
and audit path are inspected directly.

The capability inventory cache is intentionally short-lived (60 seconds) and
refreshes when the repository commit changes. Historical Debian logs do not
contain the two reported false “checkin unavailable” incidents or process-
lifetime evidence, so their original root cause remains unverified.
