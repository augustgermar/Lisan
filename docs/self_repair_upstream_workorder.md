# Work Order — Upstream Publication of Self-Repair Proposals (WO-REPAIR-UPSTREAM)

**Status: NOT EXECUTED.** Scoped 2026-11-09 in conversation between August
and Claude (Sonnet 5), as an extension of the shipped self-repair loop
(`docs/self_repair_workorder.md`, Phases A/B/C all live). Nothing in this
document is implemented. Where this document conflicts with the code as
it then exists, reality wins — report the conflict.

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
- The owner has an upstream repository to publish to and has decided which
  one (this repo, a dedicated `lisan-proposals` staging repo, or forks —
  see §3.1).
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
`writer_skeptic_graph.py`. No model call is in this path. If a class has no
template, publication is refused for that proposal rather than falling
back to a free-text description.

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
fails either one is never published; it remains available for local apply
only, and the refusal reason is recorded in the proposal report (not sent
anywhere).

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

### 2.7 Bounded appetite

Publication inherits the existing one-open-proposal-at-a-time and
one-applied-patch-per-week appetite limits (`docs/self_repair_workorder.md`
§2.3) for local apply; it does **not** get its own separate, looser budget.
A system that is thrashing locally should not be allowed to thrash in
public instead.

## 3. Open implementation questions (resolve against the code, then)

1. **Upstream target:** publish against this repository directly, or a
   dedicated low-stakes staging repo (e.g. `lisan-proposals`) that
   periodically feeds reviewed PRs back into this one? A staging repo adds
   a hop but means a redaction bug never touches the canonical repo's
   history at all. Lean: staging repo for v1, promote to direct publication
   once the redaction gate has a track record.
2. **Credential scope:** publication needs its own GitHub credential,
   scoped to opening branches/PRs only (not admin, not force-push, not
   repo settings) — follow the per-repo credential-helper pattern already
   set up for this install (`git-credential-augustgermar.sh`) rather than
   reusing the owner's personal token.
3. **Denylist completeness:** the vault-leakage check in §2.3 is only as
   good as the entity index it draws from. Decide whether to also scan
   against the vault's raw file content (slower, more complete) or stay
   index-only (fast, matches what `deviations.py` already reads). Lean:
   index-only for v1, with a documented gap; raw-content scanning is a
   natural v2 hardening pass once the mechanism is proven.
4. **Template coverage:** a deviation class with no public template (a new
   class added later without updating `public_rationale.py`) must refuse
   publication, not degrade to a generic description assembled from
   whatever fields exist — resolve the refusal-vs-degrade question
   explicitly in code with a test, not by omission.
5. **Dedup false negatives:** `gh pr list --search` matching by file set
   will miss semantically identical fixes that touch different files on
   different installs (e.g. a shared bug surfaced through two different
   code paths). Decide whether this is acceptable noise (a human closes
   the duplicate on sight) or needs a stronger signal (e.g. comparing
   normalized patch content).

## 4. Definition of done

- `public_rationale.py` exists with one template per current deviation
  class, tested against planted fixture deviations (invented cast) to
  confirm no entity-identifying content can reach the template output.
- `assert_no_vault_leakage` tested against a fixture vault with planted
  real-looking entity names: a redacted PR body containing a planted name
  is refused; one that doesn't is allowed through.
- `public_rationale.py` is in `self_repair.py`'s `PROTECTED_BASENAMES`,
  with a regression test proving a self-repair patch cannot modify it.
- `self_repair_publish_upstream` is registered in `action_policy.py`,
  reachable at its own tier, provably inert when disabled.
- Dedup check tested against a mocked `gh pr list` response (match found →
  comment, not a new PR; no match → new PR).
- End-to-end test: a verified Phase A proposal → redacted PR opened against
  a disposable test repository (never the real upstream in tests) → report
  updated with the PR URL.
- A live dry run publishes exactly one real proposal to the chosen upstream
  target, read back by the owner before any second proposal is permitted to
  publish.

---

*Origin: designed 2026-11-09 in conversation between August and Claude
(Sonnet 5), prompted by a question about whether self-repair proposals
could be reviewed by other installs and humans via GitHub rather than only
the owner's own Telegram approval. The core design decision — redact by
reconstructing from structural fields through a closed template, then
verify the reconstruction with an independent denylist scan, rather than
asking a model to anonymize free text — follows the same pattern already
proven in `writer_skeptic_graph.py`'s provenance firewall. Should feel
familiar to implement: the dangerous decisions (what may never be said,
what gates what) are made here, in advance, on purpose.*
