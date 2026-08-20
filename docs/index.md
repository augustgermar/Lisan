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

---

## Architecture at a glance

Everything lives in **the vault** — a directory of markdown records with
JSON frontmatter (entities, episodes, claims, evidence, state,
knowledge, decisions, open loops, patterns, predictions, schedules,
confirmations) plus a SQLite index and embeddings that can always be
rebuilt from the files.

**The capture pipeline** is a staff of agents: the Listener classifies
each turn, the Writer distills it into typed records, the Skeptic reviews
what was written before anything durable lands, and the Interlocutor
speaks back to you.

**Retrieval** fuses three lanes — SQL, FTS5, and embedding similarity —
with reciprocal-rank fusion, compartment enforcement, and a serendipity
slot.

**Maintenance** runs as organs on a jobs queue: the Dreamer compacts and
reconciles, the Analyst scans for behavioral patterns, deviation scans
hunt defects, and a weekly self-evaluation judges real transcripts
against a rubric derived from the identity kernel.

**The execution layer** (Adjutant) closes the loop between remembering
and doing: it polls for actionable records, gates every action against
`primer/intent.md` (the owner's authority document), executes within
granted authority, and reports results through the same capture pipeline.

**The self-repair loop** lets a detected defect graduate into a proposed,
verified, owner-approved patch — with a 48-hour bake period and automatic
rollback on regression. The agent diagnoses and drafts the treatment; the
owner remains the physician of record.

---

## Quick start

```bash
curl -fsSL "https://raw.githubusercontent.com/augustgermar/Lisan/main/install.sh" | bash
lisan init          # create the vault, seed the primer
lisan chat          # talk; memory capture runs in the background
lisan self state    # what the agent knows about its own health
```

When you're ready to let it act:

```bash
lisan intent edit           # adopt the authority document
lisan adjutant run          # one cycle: poll -> gate -> execute
lisan adjutant daemon       # the cycle on an interval
```

Configuration lives in `config.json` (see `config.example.json`);
providers are pluggable — hosted APIs, a local HTTP endpoint, or a
coding-agent CLI as the executor.

---

## Documentation map

| Section | What it covers |
|---------|---------------|
| [Security](security.md) | Trust invariants and the threat model |
| [Specification](spec.md) | The memory system's binding spec |
| [Design Documents](adjutant_workorder.md) | Sealed work orders: every settled design decision |
| [Operations](adjutant_daemon.md) | Running the Adjutant, the skills platform, the jobs system |
| [Feature Inventory](feature_inventory.md) | Exhaustive capability list |
| [Changelog](changelog.md) | Release history |

---

## Design principles

1. **Deterministic first.** An LLM is the last resort, never the first.
2. **Plain files, owned by the user.** Markdown + SQLite, readable with anything.
3. **Examiner ≠ examinee.** The system never judges its own work.
4. **Never lose data in the name of tidiness.** Compaction is lossy only in the rendering.
5. **Instruments before rules.** Every honesty rule is backed by a generated source of truth.
6. **The suite is the floor.** Contracts are pinned by tests.

---

## License

MIT. Your memories are yours.

## Provenance

Designed and built in 2026 by August Germar in collaboration with
Anthropic's Claude — chiefly Opus 4 and Fable 5 — as a working answer to
a question both authors cared about: what can one person and a frontier
model actually build together?
