# docs/ — what is live and what is history

Three documents are LIVE (unexecuted work orders; each states its own
entry gate — read it before starting):

- `psyche_workorder.md` — WO-PSYCHE, the psychological pattern layer:
  three-tier provenance (facts / owner-ratified frameworks / earned
  hypotheses), observation-first, prediction-scored. Ship 1 shipped
  2026-07-08; Ships 2 and 4 have no entry gate; Ship 3 waits for four
  weeks of observation data (clock starts when check-ins start).
- `ship2_enable_workorder.md` — enable person enrichment (Ship 2 of
  WO-ENRICH). Waits for the owner's calibration verdict, pasted into the
  slot in that file. Binding spec: `ship2_person_enrichment.md`.
  NOTE 2026-07-15: the deviation drive was silently dead 07-05→07-15
  (post-turn seam bug, fixed); the calibration period genuinely starts
  from that fix, not from Ship 1's commit date.
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
