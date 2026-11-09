# Work Order — Upstream Publication of Self-Repair Proposals (WO-REPAIR-UPSTREAM)

**Status: NOT EXECUTED, SCOPED AND DECIDED.** Scoped 2026-11-09 in
conversation between August and Claude (Sonnet 5), as an extension of the
shipped self-repair loop (`docs/self_repair_workorder.md`, Phases A/B/C all
live). All five open implementation questions in §3 were answered by August
on 2026-11-09 and the settled policy in §2 updated to match. Nothing in
this document is implemented. Where this document conflicts with the code
as it then exists, reality wins — report the conflict.

**One-line goal:** let a verified Phase A proposal also be opened as a pull
request against a public upstream repository — so other installs, other
agents, and human maintainers can review it — without ever publishing
anything that could identify the owner, their relationships, or the
specifics of their vault.

**Governing instinct:** publication is a one-way door. A local self-repair
mistake is recoverable (Phase C rolls it back); a leaked name in a public
GitHub comment is not — the comment can be deleted, but search engines,
forks, and clones do not forget. Every decision below is shaped by that
asymmetry: redaction must be structural and fail closed, never a prompt
asking a model to be careful.

---

## 1. Entry gate (all required before work begins)

- Self-repair Phases A/B/C are live and have processed at least one real
  proposal on the resident install (they have: `docs/self_repair_workorder.md`
  is marked shipped).
- The owner has an upstream repository to publish to. **Decided:** this
  repo, `augustgermar/Lisan`, directly — not a staging repo (see §3.1).
- The owner affirmatively wants this. Re-read this document together first,
  the same as the original self-repair work order required.

## 2. Settled policy (implement, do not relitigate)

### 2.1 Scope and relationship to local apply

Upstream publication is a **separate, orthogonal action** from local apply.
An owner may:
- apply a proposal locally without publishing it (default, and the only
  behavior that exists today);
- publish a proposal upstream without applying it locally (the fix isn't
  wanted on this install, but is believed to be generally useful);
- do both, independently, in either order.

Publishing never implies applying, and applying never implies publishing.
Each requires its own tier and its own confirmation.

### 2.2 What may be published

Only a Phase A proposal that has already passed independent verification
(full suite + targeted probe, examiner ≠ examinee — the existing
`propose()` contract) is eligible. No new verification standard is
introduced; publication adds a redaction gate on top of the existing one.

### 2.3 The redaction pipeline (the structural core of this work order)

Two functions, new module `lisan/tools/public_rationale.py`:

**`build_public_rationale(deviation, patch_paths) -> PublicRationale`**
Builds the PR title and body from **structural fields only**:
`deviation["klass"]`, record kinds involved, counts, collision basis, and
the changed file paths from the verified diff. It is **never given**
`loop["summary"]`, `loop["description"]`, `deviation["links"]`, any entity
id, or any vault path content. Each deviation class
(`cross_kind`, `dangling`, `thin`, `stale`, `interocept`, `capture_defect`,
`skill_health`) maps to one fixed, parameterized template — a closed
vocabulary, the same pattern as `structure_skeptic_feedback` in
`writer_skeptic_graph.py`. No model call is in this path. **Decided (§3.4):**
if a deviation class has no specific template (e.g. a new class added later
without an update to this module), publication does not refuse outright —
it degrades to a **generic structural template**, built from the exact same
closed field set as every other template (`deviation["klass"]` as a raw
string, record kind, counts, and the changed file paths), with no
class-specific phrasing. The generic template is still composed from
structural fields only — it is never a free-text fallback, and it passes
through `assert_no_vault_leakage` exactly like any other template output.
"Degrade" means "say less, more generically," never "say more, less
carefully."

**`assert_no_vault_leakage(text, vault) -> None`**
A second, independent check that does not trust the first function to have
gotten it right. Scans the assembled PR title, body, commit message, and
the diff text itself against a denylist built live from the vault: every
entity name and alias in the index, every path under `vault/`, and
regex classes for email addresses and phone numbers. Any match raises
`SelfRepairRefused` — fail closed, no partial redaction, no "publish what's
left." This mirrors `validate_patch_paths` and
`feedback_contains_factual_prose`: a structural gate, not a prompt
instruction.

Both functions run before anything leaves the machine. A proposal that
fails `assert_no_vault_leakage` — whether the text came from a
class-specific template or the generic fallback — is never published; it
remains available for local apply only, and the refusal reason is recorded
in the proposal report (not sent anywhere). The generic fallback template
changes what gets said when a class is unrecognized; it does not relax the
leakage check that gates everything regardless of which template produced
it.

`public_rationale.py` is added to `PROTECTED_BASENAMES` in
`self_repair.py`. The repair loop may never modify the mechanism that
decides what it is permitted to say about itself — the anti-self-
amplification rule extends to the publication boundary exactly as it
already covers the apply boundary.

### 2.4 The publish pipeline

1. **Dedup check:** search the upstream repo (`gh pr list --search`, by
   changed-file set and deviation class) for an existing open PR covering
   the same defect. If found, post a corroboration comment with this
   install's independent verification result (suite + targeted probe,
   pass/fail, no other detail) rather than opening a duplicate PR.
2. **Branch and push:** push the already-verified worktree (unchanged —
   same commit that passed Phase A) to a branch named from the proposal id
   (`self-repair/sr-<date>-<hash prefix>`), never the owner's main working
   branch.
3. **Open PR:** `gh pr create` with the redacted title/body from §2.3,
   labeled `self-repair` and `origin:install-<install_id>` (see §2.5).
   Never auto-merged — the PR enters ordinary human (or reviewer-agent)
   review on the upstream repo like any other contribution.
4. **Record:** the PR URL, branch name, and publish timestamp are written
   back into the proposal report under a new `upstream` key. Nothing about
   the publish event is narrated into a self-episode beyond "published
   proposal X upstream as PR #N" — no rationale duplication outside the
   PR itself.

### 2.5 Anonymous install attribution

A stable `install_id` (random, generated once at install time, never
derived from any personal or network-identifying value) is stored in
config and used only as a label and a PR footer line
(`Install: install-<id> (anonymous, not reversible to any identity)`).
It exists to let dedup and corroboration work across many installs without
identifying any of them to each other or to a reader of the PR.

### 2.6 Gating and the key

A new `action_policy` kind, `self_repair_publish_upstream`, registered
separately from `self_repair_apply` and at its own tier (candidate: tier 4,
co-gated with apply, but administratively distinct — raising one clamp
must not silently raise the other). Default: not reachable. A durable
confirmation (the existing `adjutant_confirmations` mechanism) is required
per publish, carrying the already-redacted PR title and body — never the
raw loop summary — so the confirmation itself cannot be the leak.

**Decided (§3.2):** this install publishes using the resident owner's
already-configured per-repo GitHub credential (the `augustgermar`
credential helper set up in this session) — no separate narrowly-scoped
token is introduced for this install. This is a **per-install
configuration choice, not a hardcoded one**: Lisan is open source, and
every other install publishes under whatever GitHub credential its own
owner has configured locally. The publish pipeline must read its
credential from config/environment the same way the rest of the repo's
git/GitHub tooling does — it must never assume `augustgermar` or any other
specific account is the one running.

### 2.7 Bounded appetite

Publication inherits the existing one-open-proposal-at-a-time and
one-applied-patch-per-week appetite limits (`docs/self_repair_workorder.md`
§2.3) for local apply; it does **not** get its own separate, looser budget.
A system that is thrashing locally should not be allowed to thrash in
public instead.

## 3. Implementation questions — decided 2026-11-09

All five questions below were open when this document was first drafted.
August answered each in conversation on 2026-11-09; decisions are recorded
here and reflected in the settled policy in §2.

1. **Upstream target — decided: direct to this repo.** Publish against
   `augustgermar/Lisan` directly, as a branch and a PR — never
   auto-merged, never pushed straight to a branch anyone treats as
   mergeable-on-sight. No staging repo. This matches the pipeline already
   specified in §2.4 (push a named `self-repair/sr-...` branch, open a PR,
   leave it for ordinary review) — the redaction gate is the safety
   mechanism, not a second repo standing between the gate and the real
   history. If the redaction gate's track record later argues for more
   distance, that would be a deliberate future revision, not the v1
   default.
2. **Credential scope — decided: reuse, per-install.** This install
   publishes using the already-configured `augustgermar` credential
   helper; no separate narrow-scope token for this install specifically.
   Because Lisan is open source, this is **not** a global design decision
   to hardcode one account — it is this install's local configuration.
   The publish pipeline must read "which GitHub credential does this
   install use" from the same per-repo config/credential-helper mechanism
   every other git/GitHub operation in this codebase already uses, so a
   different install, with a different owner and a different GitHub
   account, publishes under its own credential without any code change.
3. **Denylist completeness — decided: index-only for v1.** Scan the live
   entity index (names, aliases) plus email/phone regex classes; do not
   also scan raw vault file content. Documented gap: an entity mentioned
   in vault text but not yet captured as an indexed entity could slip
   through. Raw-content scanning remains a natural v2 hardening pass once
   the mechanism has a track record.
4. **Template coverage — decided: degrade, do not refuse.** A deviation
   class with no specific template falls back to a generic structural
   template built from the same closed field set (klass name, record
   kind, counts, changed file paths) rather than refusing publication
   outright. See the updated §2.3 for the exact constraint: the generic
   template is still structural-only and still passes
   `assert_no_vault_leakage` — "degrade" narrows what is said, it never
   loosens how it's checked.
5. **Dedup false negatives — decided: accept as noise for v1.** Keep the
   file-set-based `gh pr list --search` matching from §2.4 as-is. Missed
   semantically-identical fixes across installs that touch different
   files are acceptable v1 noise; a human closes the duplicate on sight.
   A stronger signal (e.g. normalized patch-content comparison) is a
   future hardening pass, not a v1 requirement.

## 4. Definition of done

- `public_rationale.py` exists with one template per current deviation
  class plus the generic fallback template (§2.3, §3.4), tested against
  planted fixture deviations (invented cast) to confirm no
  entity-identifying content can reach the output of any template,
  including the fallback.
- A test proves an unrecognized/invented deviation class routes to the
  generic fallback template (not a refusal, not free text) and that the
  fallback's output still passes `assert_no_vault_leakage`.
- `assert_no_vault_leakage` tested against a fixture vault with planted
  real-looking entity names: a redacted PR body containing a planted name
  is refused; one that doesn't is allowed through. This must hold for both
  class-specific and generic-fallback template output.
- `public_rationale.py` is in `self_repair.py`'s `PROTECTED_BASENAMES`,
  with a regression test proving a self-repair patch cannot modify it.
- `self_repair_publish_upstream` is registered in `action_policy.py`,
  reachable at its own tier, provably inert when disabled.
- The GitHub credential used for publishing is resolved from this
  install's own per-repo config/credential-helper mechanism, not
  hardcoded to any account name — a test proves swapping the configured
  credential changes which identity publishes, with no code change.
- Dedup check tested against a mocked `gh pr list` response (match found →
  comment, not a new PR; no match → new PR).
- End-to-end test: a verified Phase A proposal → redacted PR opened against
  a disposable test repository (never the real upstream in tests) → report
  updated with the PR URL.
- A live dry run publishes exactly one real proposal to `augustgermar/Lisan`
  as a branch + PR (never auto-merged), read back by the owner before any
  second proposal is permitted to publish.

---

*Origin: designed 2026-11-09 in conversation between August and Claude
(Sonnet 5), prompted by a question about whether self-repair proposals
could be reviewed by other installs and humans via GitHub rather than only
the owner's own Telegram approval. The core design decision — redact by
reconstructing from structural fields through a closed template, then
verify the reconstruction with an independent denylist scan, rather than
asking a model to anonymize free text — follows the same pattern already
proven in `writer_skeptic_graph.py`'s provenance firewall. The five open
questions in §3 were decided the same day, also by August: publish
directly to `augustgermar/Lisan` as a branch + PR; reuse this install's
own configured GitHub credential, as a per-install choice rather than a
hardcoded one, since Lisan is open source and other installs will use
their own; stay index-only on the leakage denylist for v1; degrade to a
generic structural template rather than refuse when a deviation class has
no specific template; and accept file-set dedup noise for v1. Should feel
familiar to implement: the dangerous decisions (what may never be said,
what gates what) are made here, in advance, on purpose.*
