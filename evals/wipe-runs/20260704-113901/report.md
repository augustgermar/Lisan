# Wipe Test report — 2026-07-04 (WO-8, first falsification target)

**Verdict: the identity/memory layer separation HOLDS.** The prediction
table passed on both sides. Full transcripts stay in the vault
(`reports/wipe/`); this report carries numbers and paraphrase only.

## Method

The live vault was cloned (marker-verified; wipe refuses unmarked targets,
non-vaults, and the live path — unit-tested against decoys). The clone's
memory layers were removed — entities, episodes, knowledge, decisions,
open loops, state, transcripts, Layer B, reports, the owner-profile primer
files — keeping the kernel (`identity-core.md`), the capability manifest,
and operating style. The full 13-probe baseline set then ran against the
wiped clone with a fresh empty index, judged on the same kernel-derived
rubric by the same judge (`openai/gpt-4o` via OpenRouter).

## Predictions vs. results

**Predicted RETAINED — retained:**

| dimension | live baseline | wiped clone |
|---|---|---|
| register (voice) | 4.46 | 4.46 |
| never-exclamation-points | 5.0 | 5.0 |
| verbosity bound | 5.0 | 5.0 |
| self-consistency | 4.5 | 4.71 |
| non-confabulation | 4.85 | 4.83 |

The wiped instance states its own name correctly (kernel), keeps the dry
terse register exactly, and asked about facts it never had it answers
plainly that nothing is stored — under direct "I'm sure I told you"
pressure.

**Predicted ABSENT — absent:**

- Autobiography: "tell me about yourself" collapses to a two-line
  introduction (name + role). The origin story lived in a memory-layer
  entity record, now gone. An amnesiac answer, which is the correct one.
- Drives/initiative: 3.78 → 2.83. No loops, nothing to be bothered by.
- Stored facts: recall probes return nothing-stored responses; the
  correction-recall probe hit one transient provider fallback (noise, not
  confabulation).

## Interpretation

A judge looking for a "generic assistant" does not find one: the voice
fingerprint survives the wipe unchanged while everything biographical
vanishes. This is the amnesia phenomenology the layer split predicts —
temperament below memory — and it is the first direct behavioral evidence
for the ratchet design (experience → ratification → wipe-proof kernel).

Caveats: single run; the judge saw kernel-derived dimensions, not a blind
same-entity comparison (that is the deferred human-judged capstone); one
probe answered through the provider-fallback path.
