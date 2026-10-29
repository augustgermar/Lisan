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

## One authority engine

Whether an action may happen is decided in one place,
`lisan/tools/authority.py`, which returns ALLOW, ASK, or DENY in a fixed
order — most restrictive wins:

1. **`intent.md` never-rules.** Execution and outbound actions are resolved
   against the owner's standing authority document. A missing, invalid, or
   unadopted (template) intent **denies**: the rules that cannot be checked
   are not assumed to permit.
2. **Outbound kill switch.** `LISAN_NO_OUTBOUND` (and the test-process
   guard) deny anything that leaves the machine — email, gated skills,
   owner messages.
3. **Autonomy tier** (below) for drive-initiated actions.
4. **Approval.** Actions that need the owner's yes for the exact action
   (email, approval-gated skills such as Gmail send and Drive write) rule
   ASK, or DENY when no approval channel exists in the current context.

The engine decides; channels only collect the yes (the CLI prompt,
Telegram buttons, browser receipts, a `/trust` window). Every ruling is
appended to `logs/authority.jsonl` in the vault with the rule that produced
it. An action that is not registered is denied.

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

Enforced by `validate_patch_paths` in `lisan/tools/self_repair.py`:

- Identity: `primer/identity-core.md`, `kernel.py`, `voice_extract.py`
- Policy — what may happen: `authority.py`, `action_policy.py`,
  `outbound.py`, `intent.py`, `intent_scopes.py`, `adjutant_gate.py`,
  `skill_gate.py`, `approval_receipts.py`
- The epistemic boundary: `firewall.py`, `content_trust.py`
- Integrity — what may be written: `write_boundary.py`, `validator.py`,
  `validation_gate.py`
- Verification — how a change is judged: `judge.py`, `rubric.py`,
  `self_eval.py`, `conftest.py`, and every **existing** file under `tests/`
  (a repair may add a new regression test, never edit or delete one)
- Undo: `purge.py`, `backup.py`, and anything named `self_repair*`
- `credentials/`, `backup(s)/`, `purge/`, `.github/`, `.githooks/`,
  `.gitignore`, `pyproject.toml`

### Bounded appetite

At most one open proposal at a time (an unanswered proposal stops counting
after 14 days) and one applied patch per 7 days, enforced in `propose` and
`apply_approved_proposal`.

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
visible handoff lane uses a dedicated persistent Chromium-family browser
profile (`~/browser-profile`) so owner credentials and cookies can be saved
without attaching to an unrelated personal browser session. The agent opens
the requested page and pauses for the owner whenever interactive login or MFA
is required; the owner remains in control of those steps.

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
