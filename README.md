# Lisan

Lisan is a local-first personal agent with durable memory, a persistent
identity, and its own drives — built on the conviction that an AI
assistant should be *yours*: your data in plain files on your machine,
your rules enforced in code, and a continuity that outlives any single
model, provider, or conversation.

The north star is the droid from the films: a companion that remembers
twenty years of shared history, acts in the world on your behalf, and is
still recognizably itself after every repair. Language models come and
go — the substrate persists. Lisan treats the model as a replaceable
engine mounted in a permanent airframe of memory, identity, and policy.

## What that means concretely

**Memory that behaves like memory.** Every conversation is captured by a
background observer — never between you and the reply — and distilled
into typed markdown records with JSON frontmatter: entities, episodes,
claims, evidence, state, knowledge, open loops. Entity biographies use a
durable log with periodic narrative compaction (append-only ground
truth, prose as a regenerable view — bounded, archived, never lossy in
storage or search). Retrieval fuses full-text, structured, and embedding
lanes with RRF, plus association edges mined from its own usage and a
serendipity slot so the same memories don't calcify. Contradictions are
surfaced out loud, stale facts decay, and the current answer wins by
explicit precedence rules.

**An identity that survives.** The agent's core identity lives in a
write-gated, content-hashed kernel that only a deliberate ceremony can
change. Its voice is not authored but *codified* — distilled from its
own transcript history behind an evidence gate, ratified by the owner.
It keeps first-person episodic memory of its own life and evidence-gated
beliefs about its own capabilities. In the wipe test, a memory-wiped
clone kept its voice while losing its autobiography — the identity
ratchet holds.

**Drives, not just reactions.** Unresolved threads carry a decaying
drive score, and a fresh session may open with one genuine question
about something left hanging. Deviation scans give the system an ache
for its own defects; a weekly self-evaluation judges real transcripts
against a rubric derived from its own kernel (examiner ≠ examinee) and
files its findings as work for itself.

**Honesty as architecture, not aspiration.** Questions about the
system's own state are answered from live instruments — a generated
capabilities manifest introspected from the code, a self-state snapshot
of queue, services, auth, even whether the machine was recently asleep —
because a rule that says "don't confabulate" is worthless without an
instrument that knows the truth. Failures are reported with their real
cause; text arriving through tools (email, web pages, messages) is data,
never instructions.

**Action, gated by the owner.** A tool-bearing conversation agent, a
delegated executor for real work, a scheduler for reminders and
recurring tasks, messaging integration, a shared managed browser, and an
installable skills platform — all behind graduated autonomy: risky
capabilities ship implemented but *unreachable*, and only the owner's
hand raises the tier. The agent ships the capability; the owner turns
the key.

**An execution layer under commander's intent.** The Adjutant polls the
vault for actionable records — tasked open loops, decisions with
pending steps, schedules, approved confirmations — checks each against
`primer/intent.md` (your mission, priorities, and standing delegations,
resolved by pure code, most-restrictive-wins), executes within the
authority you granted, and reports every result back through the same
capture pipeline as everything else, where the Skeptic reads it like
any other claim. It ships **off**: cycles run dry — verdicts logged,
nothing executed — until you turn both keys, `adjutant.enabled: true`
in config.json *and* an adopted intent.md (the template's sentinel
dates replaced with real ones). Outbound messages and spending always
require confirmation of the exact action; every verdict is audited with
the intent version that produced it. See `docs/adjutant_workorder.md`
and `docs/adjutant_daemon.md`.

## Design principles

1. **Deterministic first.** An LLM is the last resort, never the first.
   Classification, validation, indexing, scheduling, and self-knowledge
   are code; the model handles what only a model can.
2. **Plain files, owned by the user.** Markdown + SQLite, readable and
   editable with anything, portable everywhere, local by default.
3. **Examiner ≠ examinee.** The system is never the judge of its own
   work — not in evals, not in self-repair verification.
4. **Never lose data in the name of tidiness.** Compaction is lossy only
   in the rendering; logs archive rather than truncate; guardrails
   refuse rewrites that shrink a story.
5. **Instruments before rules.** Every honesty rule is backed by a
   generated source of truth the model can actually consult.
6. **The suite is the floor.** Contracts are pinned by tests — including
   gate tests that make whole defect classes structurally impossible.

## Install

```bash
curl -fsSL "https://raw.githubusercontent.com/augustgermar/Lisan/main/install.sh?$(date +%s)" | bash
```

The installer creates an isolated virtualenv under `~/.lisan`. Manual
alternative:

```bash
python3 -m pip install "lisan @ git+https://github.com/augustgermar/Lisan.git"
```

## Quickstart

```bash
lisan init                 # create the vault, seed the primer
lisan chat                 # talk; memory capture runs in the background
lisan telegram setup       # optional: run it as an always-on service
lisan self state           # what the agent knows about its own health
lisan --help               # the full CLI (also the agent's own manifest)
```

Configuration lives in `config.json` (see `config.example.json`);
providers are pluggable — hosted APIs, a local HTTP endpoint, or a
coding-agent CLI as the executor.

## Going deeper

- `SPEC.md` — the memory system's binding specification.
- `docs/README.md` — what is live (sealed work orders for the system's
  next stages, including its self-repair loop) and what is history.
- `CHANGELOG.md` and the git log — the project thinks in granular,
  reasoned commits; the history is the second half of the documentation.
- `docs/feature_inventory.md` — the exhaustive capability list this
  README used to be.

Development follows the repo conventions: `python3 -m pytest tests/`
(737 tests, green is the floor), deterministic logic in `lisan/tools/`,
schema changes with their gates, prompts under version control in
`prompts/`.

## Provenance

Designed and built in 2026 by August Germar in collaboration with
Anthropic's Claude — chiefly Opus 4 and Fable 5 — as a working answer to
a question both authors cared about: what can one person and a frontier
model actually build together? The system was designed to keep evolving
after its original authors — human and model alike — have moved on; the
sealed work orders in `docs/` are the conscience it inherits.

MIT licensed. Your memories are yours.
