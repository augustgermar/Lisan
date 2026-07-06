# Work Order — Release Readiness (WO-RELEASE)

**Status: SPECIFIED, NOT SCHEDULED.** For whenever the owner decides to
make the repo public-facing (README audience beyond himself). Ordered by
value; each item independently shippable.

1. **Stranger-installability.** Remove machine-specific absolute paths
   (evals/scale_*.py, eval tooling; audit with `git grep '/Users/'`).
   Providers configurable down to a single API key with graceful
   degradation when optional pieces (telegram, codex, playwright, local
   proxy) are absent. `lisan init` verified on a clean machine by
   actually following INSTALL.md. Generate launchd plists from a command
   rather than shipping hand-edited ones.
2. **README + docs site.** Short README: thesis (memory + action +
   identity continuity), ten-line architecture, honest research-project
   framing, MIT license badge (already licensed MIT — say so), and a
   pointer into docs/. Convert docs/ design documents into browsable
   HTML (mkdocs-material or equivalent; keep sources as the .md files —
   the design docs ARE the flagship content).
3. **Security page.** State the invariants users are trusting: allowlist
   -locked owner channel, localhost-only browser control, executor write
   boundary, action-policy tiers ("nothing leaves the vault unprompted"),
   external-content-is-data rule, and what each config raise (tier,
   enrichment) actually changes.
4. **External-content fencing, complete.** The conversation rule and
   browser fencing exist; extend the fence wrapper to all skills that
   return third-party-authored text (mail, messages, transcripts), and
   add writer-prompt guidance so ingested imperatives are stored as
   content, never absorbed as behavioral contracts.
5. **Release hygiene**: pinned dependencies, CI badge, version tags,
   contribution stance in README, a fresh-clone review of all shipped
   evals/docs prose by a reader who has never seen the project.
