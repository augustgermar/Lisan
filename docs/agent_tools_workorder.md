# Work Order — Tool Access Beyond the Conversation (WO-AGENTTOOLS)

**Status: SPECIFIED, NOT SCHEDULED.** Raised 2026-08-22 after the
two-lane browser landed and the question "which agents can use it?"
turned out to have a narrow answer.

## The finding

Two agents hold tools: the **Interlocutor** and the **Conversation**
agent. Both obtain them the same way, through `agent_tools()` and
`build_tool_handlers()` in `lisan/tools/execution_tools.py`. Everything
else in `lisan/agents/` — Listener, Writer, Skeptic, Analyst,
Self-Analyst, Dreamer, Assembler, Elicitor, Router, Advice,
Voice-extractor — is a single-purpose LLM call over text, with no tools
at all.

Three code paths reach the quiet browser lane without being agents,
because `installed_published_providers()` returns it as the default web
backend: `librarian`, `enrichment.seek`, and `hypothesis_research`. They
can search and fetch. They cannot hand off to the owner.

The **Adjutant cannot browse at all.** Its `research` task kind
delegates to the generation provider's own web access (the codex CLI),
a separate path with none of the session, cookies, or handoff:

```python
"""Research via the provider (which may carry its own web access, e.g.
the codex CLI)."""
```

So the agent that runs unattended — the one that would benefit most from
a browser that never touches the owner's keyboard — is the one that
cannot use it. Tools are available to the agent the owner is talking to
*while they are sitting there*, and unavailable to the one working while
they are not.

## The argument for

**The capability is already built and idle.** The quiet lane exists,
carries the owner's session, and cannot steal focus. Withholding it from
the Adjutant does not prevent autonomous web access — the Adjutant
already researches through the provider's own web tooling. It prevents
*supervised, auditable* web access, and leaves the unaudited path in
place. That is the wrong way round.

**Handoff makes unattended work resumable rather than failed.** Today an
Adjutant task that meets a login wall dies. With handoff it becomes a
question: the owner gets a Telegram message, does the thing, and the
task continues. Every "blocked, needs the owner" failure is a candidate.

**Other agents are blind in ways that matter.** The Analyst reasons about
patterns it cannot check against the world. The Skeptic reviews claims
without being able to verify a single one. Deviation scans hunt defects
with no way to read a changelog. A read-only fetch would change what
each can conclude.

**The gate already exists.** `intent.md`, the action tiers, and the
confirmation flow were built for exactly this: capability shipped and
unreachable until the owner turns a key. Browsing is a smaller step than
several already taken.

## The argument against

**Tools change what an agent is.** Every agent listed above is a pure
function from text to text. That is why they are testable, cheap, and
predictable, and why a bad output is contained. An agent that can act
mid-inference is a different object: it can loop, spend, and surprise.
The 2026-07-27 plan recursion — 234 plans from one request — happened in
a system with far fewer moving parts.

**The Skeptic must not become a participant.** Its value comes from
examining what was written, not from gathering new material. An agent
that can fetch evidence for a claim it is judging has stopped being an
examiner. This collides directly with **examiner ≠ examinee**, and the
collision is not fixable by prompt wording.

**Unattended browsing is a wider blast radius than unattended scripting.**
The quiet lane carries the owner's real cookies: Gmail, Drive, banking,
anything they have logged into. A scripted task runs in a scratch dir
with an allowlist. A browsing task with a live session is authenticated
as the owner everywhere, and prompt injection from a fetched page is the
documented attack. Text arriving through tools is already treated as
data, never instructions — that rule now has to hold against a page the
agent chose to visit while nobody was watching.

**Handoff assumes an owner who is present.** The Adjutant runs on a
schedule, including overnight. A task that opens a window and waits will
wait until it times out, having put a window on the owner's screen at
3am. Handoff was designed for a conversation, and the Adjutant is not
having one.

**Cost and quota are per-call and unmetered.** The browser lane holds no
budget. An agent that browses in a loop is a slow, expensive failure that
nothing currently stops.

## If it proceeds

1. **Read-only first.** `goto`, `read`, `search`. No `click`, no `type`,
   no `handoff`. Most of the value, little of the risk.
2. **Adjutant before organs.** It has a gate, an intent document, and an
   audit trail. The Dreamer and Analyst have none of those.
3. **A separate lane for unattended work**, with its own profile and no
   copied cookies. The session is what makes browsing dangerous while
   nobody is watching; unauthenticated browsing is a different risk.
4. **Never the Skeptic.** Examiner ≠ examinee is a settled ruling, not a
   default to revisit.
5. **A per-cycle page budget**, logged, and a halt that says why.
6. **Handoff only when the owner is demonstrably present** — a recent
   Telegram turn, not a clock.

## Open questions

- Does an Adjutant browsing task need its own task kind, or is it a
  capability of `research`?
- If the unattended lane carries no cookies, is it materially better
  than the provider's existing web access — and if not, is the honest
  answer to route provider research through our own lane so it is at
  least audited?
- What does the Skeptic do when it needs a fact it cannot fetch? Today
  it flags the gap. Is that the right answer, or a limitation we have
  learned to live with?
