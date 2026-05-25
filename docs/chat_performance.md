# Chat Performance And Identity

This page covers the fast path for trivial turns, the turn trace commands, and the guardrails that keep production chat from drifting into eval personas or retrieved identities.

## Fast Path

Trivial chat turns are classified before retrieval and before the memory pipeline.

Fast-path examples:

- `hi`
- `hello`
- `thanks`
- `ok`
- `what is your name?`
- `who are you?`
- `what are you?`
- `help`
- `what are you up to?`
- short acknowledgments and casual small talk

Fast-path turns:

- do not run Writer
- do not run Skeptic
- do not run Analyst
- do not run Dreamer
- do not run graph retrieval
- do not queue background analysis jobs

Most fast-path turns are answered deterministically. Identity questions always resolve to:

> My name is Lisan. I am your local personal assistant and memory system.

## Expected LLM Calls

- Deterministic fast-path turn: `0` calls
- Simple direct advice turn: usually `1` call
- Memory capture turn: multiple calls are expected
- If the provider fails before it can answer, chat now returns an explicit provider error instead of falling back to generic identity-contaminated text.

If a trivial turn takes several seconds or shows multiple calls, inspect the trace output first.

## Trace Mode

Run chat with tracing enabled:

```bash
python3 -m lisan chat --trace
```

After each response, Lisan prints a compact summary such as:

```text
trace: fast_path=true, llm_calls=0, retrieval=0, jobs=0, elapsed=3ms
```

The trace is also stored in SQLite and in `logs/traces/<turn_id>.json` under the vault.

## Inspecting Traces

Recent traces:

```bash
python3 -m lisan traces recent
```

Show one trace:

```bash
python3 -m lisan traces show <turn_id>
```

Use the trace output to check:

- which route the turn took
- whether retrieval ran
- which inline steps happened
- how many model calls occurred
- whether any call failed
- whether a provider call failed before the turn could complete

## Diagnosing Slow Turns

If a simple turn feels slow:

1. Run `python3 -m lisan chat --trace`.
2. Check whether the turn was classified as fast-path.
3. If it was not, inspect `python3 -m lisan traces show <turn_id>`.
4. Look for multiple inline model calls, retrieval, or queued jobs.
5. If the turn was supposed to be trivial but was classified as memory, adjust the classifier before changing the memory pipeline.

## Provider Preflight

If you are seeing provider failures on startup or during live evals, run:

```bash
python3 -m lisan provider check
```

For the local Codex provider, the most common issue is permissions on `~/.codex/sessions`. A typical fix is:

```bash
mkdir -p /Users/august/.codex/sessions
chmod 700 /Users/august/.codex /Users/august/.codex/sessions
chown -R august:staff /Users/august/.codex
```

If the provider check fails, live evals should report infrastructure failure separately from any behavioral scoring.

## Identity Contamination

Production chat is guarded against eval and retrieved-person contamination.

Lisan will refuse to start chat if:

- the vault path is inside `.lisan_eval_runs`
- an eval marker file exists in the vault root

If a retrieved record mentions a person named `Alice`, `Tia`, `Steve`, or anyone else, that record is still just data about the user's world. It must never override assistant identity.

If identity looks wrong:

1. Check the trace to confirm whether the turn used fast path.
2. Confirm that the response came from the identity rule, not from retrieval.
3. Verify the vault is not an eval vault.
4. Confirm the prompt includes the identity anchor in `prompts/advice_v1.md` and `prompts/interlocutor_v1.md`.
