# docs/ — what is live and what is history

`skills.md` is a STANDING REFERENCE, not a work order: the Agent Skills
format (`SKILL.md` + frontmatter), progressive disclosure, the two kinds of
skill, and where they live. Skills themselves are gitignored, so that doc
and the loader are what a downloader actually gets.

Four documents are LIVE (work orders in flight or awaiting their entry
gate — read the gate before starting):

- `adjutant_workorder.md` — WO-ADJUTANT, the execution layer +
  commander's intent. CODE COMPLETE 2026-07-24 (steps 1–7); LIVE only
  for its calibration soak — dry-run verdicts accumulate in
  adjutant_log until the owner's audit earns `enabled: true`. The doc
  carries the resolved delegation contract and every settled ruling —
  it supersedes the original draft spec. Runtime guide:
  `adjutant_daemon.md`.
  **READ THE 2026-07-29 RULING FIRST** (§ "The delegation axis is
  `scope`, not `arena`"). It supersedes the word "arena" everywhere in a
  delegation sense, and it voids the first soak: that one measured a gate
  with a single reachable verdict, because no record carried any declared
  scope. **SUPERSEDED 2026-07-30:** the soak is retired as an entry gate —
  see the verdict-matrix ruling in that document. It was measuring an empty
  pipeline (3 verdicts in 6 days), and coverage is now a test that runs on
  every push. What remains before `enabled: true` is owner judgement on a
  few real taskings, not elapsed time.

- `psyche_workorder.md` — WO-PSYCHE, the psychological pattern layer:
  three-tier provenance (facts / owner-ratified frameworks / earned
  hypotheses), observation-first, prediction-scored. Ships 1, 2, and 4
  shipped (2026-07-08, 2026-07-15, 2026-07-15); only Ship 3 (the
  analyst organ) remains, gated on four weeks of observation data —
  the clock starts when check-ins start.
- `ship2_enable_workorder.md` — enable person enrichment (Ship 2 of
  WO-ENRICH). **Calibration verdict given 2026-08-14: curiosity, not
  grinding — the gate is cleared and this is ready to build.** Binding spec:
  `ship2_person_enrichment.md`, **rewritten the same day**: the owner read it
  back against its own intent and removed the four-prong permission gate,
  which by its own rules could never have fired on the loops Ship 1 produces.
  What Ship 2 builds is the system noticing a thin spot and going to close it.
  Ring 2 (the published world) is deferred behind one open decision in that
  spec's §9.
- `self_repair_workorder.md` — WO-REPAIR, the self-improvement loop.
  Waits for 4 weeks of self-eval history (same note: the weekly
  self-eval only began firing 2026-07-15; gate opens ~mid-August).

Executed 2026-07-15: `ground_truth_workorder.md` — WO-GROUND,
self-referential questions answered from generated ground truth, never
memory. Now history; it explains the self_questions detector, the
GROUND_TRUTH injection, and the self_report claim gates.

Everything else here is a historical record of executed work or settled
design (phase2_roadmap.md and its reports, exception_triage.md, ...).
Execute nothing from those; they explain why the code is the way it is.

Naming note for humans and agents alike: "Phase 2" (done, 2026-07-04)
and "Ship 2" (pending) are unrelated despite the names. Yes, we know.
