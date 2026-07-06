# docs/ — what is live and what is history

Two documents are LIVE (unexecuted work orders; each states its own entry
gate — read it before starting):

- `ship2_enable_workorder.md` — enable person enrichment (Ship 2 of
  WO-ENRICH). Waits for the owner's calibration verdict, pasted into the
  slot in that file. Binding spec: `ship2_person_enrichment.md`.
- `self_repair_workorder.md` — WO-REPAIR, the self-improvement loop.
  Waits for 4 weeks of self-eval history.

Everything else here is a historical record of executed work or settled
design (phase2_roadmap.md and its reports, exception_triage.md, ...).
Execute nothing from those; they explain why the code is the way it is.

Naming note for humans and agents alike: "Phase 2" (done, 2026-07-04)
and "Ship 2" (pending) are unrelated despite the names. Yes, we know.
