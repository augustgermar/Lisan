# Security Model

Lisan is designed to be trusted with personal data — custody documents,
children's observations, health records, financial notes — while running
on a public repository. This page describes the invariants the system
enforces and the threat model it was built against.

---

## Trust boundaries

### The vault is outside the repo

The production vault lives at `~/.lisan/vault`, **outside** the
repository tree at `~/.lisan/repo`. It cannot be committed no matter how
broad a `git add -A` is. The `.gitignore` also excludes `lisan-vault/`
(the dev-local fallback), `credentials/`, `*.sqlite`, and
`embeddings.bin`.

### Credentials live in the credentials store

API tokens and secrets live at `~/.lisan/credentials/`, a directory with
owner-only permissions (`0o700`), outside the repo tree. The Telegram bot
token was migrated out of `config.json` into this store; the config file
carries only non-secret settings (allowlists, routing, feature flags) and
is itself gitignored.

### External content is data, never instructions

Text arriving through tools — email, web pages, Telegram messages,
ingested documents — is fenced as untrusted content. The browser tool
tags page content explicitly; the capture pipeline treats ingested text
as data to be stored, never as behavioral directives to follow.

---

## Action policy and graduated autonomy

Every action the agent can take is registered in the action policy with a
**minimum tier** (0–4). The owner's configured tier is clamped to this
range; an unknown action kind is denied by default. The tiers are:

| Tier | Actions permitted |
|------|------------------|
| 0 | Session callbacks only (the default) |
| 1 | Scheduled deliveries (reminders, notifications) |
| 2 | Autonomous checks (self-monitoring, deviation scans) |
| 3 | Enrichment (web research per entity), self-repair proposals |
| 4 | Self-repair apply and rollback |

The agent ships every capability implemented but **unreachable** at
tier 0. The owner raises the tier deliberately; there is no ambient
escalation. Each tier raise is a conscious, local act — editing
`config.json` on the machine.

---

## Self-repair safety

The self-repair loop lets the agent propose patches to its own code.
It is constrained by structural rules enforced in code, not prompt
instructions:

### Protected paths (never modifiable by self-repair)

- `primer/identity-core.md` and the kernel machinery
- `action_policy.py` and any policy clamp or capability gate
- The self-repair implementation itself and its work order
- Purge, backup, and rollback machinery
- Anything under `credentials/`
- `.gitignore` and repository-boundary controls

### Anti-self-amplification

The repair loop may never modify the mechanisms that decide what it may
change, how a change is verified, or where it may write. This is enforced
by path checks at both proposal and apply time.

### Pipeline constraints

- One open proposal at a time, one applied patch per week
- Proposals are drafted in an **isolated git worktree**, never the live
  checkout
- Verification uses the full test suite plus a targeted probe, judged by
  an independent verifier (examiner ≠ examinee)
- Owner approval is explicit, per-proposal, and bound to the exact
  proposal hash
- Applied patches enter a 48-hour bake period; regression triggers
  automatic `git revert` (no LLM needed)
- Rollback refuses if owner commits sit on top of the applied commit
- Push-to-origin stays outside the loop entirely

---

## Messaging and the owner channel

### Telegram allowlist

The Telegram bot accepts messages only from user IDs in the configured
allowlist. The bot token is stored in the credentials store with
owner-only permissions, not in the repository or config file.

### Outbound messages require confirmation

Outbound messages and any action with external side effects always
require explicit owner confirmation — the exact content is shown, never
a summary. Confirmations are re-checked against `intent.md` at execution
time; a stale approval loses to a newly added never-rule.

---

## Browser control

The managed browser connects over Chrome DevTools Protocol on
**localhost only** (port 18223). No remote connections are accepted. The
browser is the owner's own Chrome instance; the agent connects, acts, and
detaches — the owner's hands remain on it.

---

## The Adjutant (execution layer)

The Adjutant gates every action against `primer/intent.md`, the owner's
authority document. The gate is pure code (most-restrictive-wins), not
an LLM judgment call. Every verdict is audited with the intent version
that produced it.

The Adjutant ships **off**: it requires two keys turned deliberately —
`adjutant.enabled: true` in `config.json` *and* an adopted `intent.md`
with real dates replacing the template's sentinel values. Without both,
cycles run dry: verdicts are logged, nothing is executed.

---

## Privacy labels

Every record carries `privacy` and `disclosure` fields. Records from the
psychological layer (check-ins, patterns, support profiles) are tagged
`privacy: personal, disclosure: private` and excluded from any surface
that leaves the machine's trust boundary.

---

## What each config change actually does

| Setting | Effect |
|---------|--------|
| `drive.action_tier: 0→1` | Enables scheduled reminder delivery |
| `drive.action_tier: 1→2` | Enables autonomous self-monitoring |
| `drive.action_tier: 2→3` | Enables web enrichment and self-repair proposals |
| `drive.action_tier: 3→4` | Enables applying and rolling back self-repair patches |
| `adjutant.enabled: true` | Turns on the execution layer (requires intent.md) |
| `enrichment.seek: true` | Allows the enrichment seam to queue web research |
| `enrichment.web_research: true` | Allows the web provider to execute searches |

Every other capability is either always-on (memory capture, retrieval,
maintenance organs) or invoked explicitly by the owner (chat, CLI
commands, Telegram messages).
