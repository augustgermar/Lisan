# Skills — the Agent Skills format

A skill is a directory with a `SKILL.md` in it. That is the whole contract, and
it is the same contract Claude Code, Codex, and the other agentic systems use,
so a skill written for any of them works here.

```
my-skill/
  SKILL.md          # required: YAML frontmatter + instructions
  references/       # optional: loaded only if SKILL.md sends the reader there
  scripts/          # optional
```

```markdown
---
name: zine-layout
description: Use when laying out a letterpress zine spread — imposition order, gutter widths, and which paper stock takes which ink.
version: 0.2.0
allowed-tools: Read, Bash
user-invocable: true
---

# Zine layout

1. Work out the imposition with `scripts/impose.py`.
2. Gutters: 14mm inner, 9mm outer.
3. For stock and ink pairings see `references/stock.md`.
```

`name` and `description` are required. Everything else is optional: `version`,
`allowed-tools` (inline or a block sequence), `user-invocable`, `license`,
`argument-hint`, `disable-model-invocation`, and a one-level `metadata` block.

## Why the description is the most important line you write

**Progressive disclosure** is the point of the format. Only `name` and
`description` stay in context — enough for the agent to judge that a skill is
relevant. The body loads when the skill is actually invoked, and files under
`references/` load only if the body sends the reader to them.

So a hundred installed skills cost a hundred one-line descriptions rather than a
hundred documents, and the description is doing the entire job of getting your
skill chosen. Write it as *when to use this*, not *what this is*.

## Two kinds of skill

|                    | Instructional            | Executable                       |
| ------------------ | ------------------------ | -------------------------------- |
| Files              | `SKILL.md`               | `SKILL.md` + `schema.json` + `tool.py` |
| How the agent uses it | calls `skill("name")`, follows the instructions | calls it as a function with JSON-Schema arguments |
| In context         | one line                 | one line **plus its parameters** |

An executable skill's parameters must be in context, because the model cannot
call a function correctly without them. That is the one thing that does not
defer.

`tool.py` exposes `run(args: dict, vault: Path, config: dict) -> str`. Declaring
`"requires_approval": true` in `schema.json` gates it at call time — use it for
anything that leaves the machine.

## Where skills live

| Location | What it is |
| --- | --- |
| `<install>/skills/` | the local catalogue, **gitignored** |
| `~/.local/share/Lisan/skills/` | the active set the agent loads |
| `$LISAN_SKILLS_DIR` | overrides the active set |

Skills are **not** version-controlled with the project. A downloader gets the
machinery — the loader, progressive disclosure, validation, migration — and
none of anyone else's procedures. Drop a directory with a `SKILL.md` into either
location and it is discovered on the next run; there is nothing to register.

## Commands

```bash
lisan skills list                  # bundled and installed
lisan skills validate              # check against the format, name the faults
lisan skills migrate [--apply]     # add frontmatter to pre-format skills
lisan skills install <name>        # copy from the local catalogue into the active set
lisan skills uninstall <name>
```

`validate` exists because the previous loader's failure mode was silence: a
directory it did not recognise was skipped without a word, so a skill you
believed was installed simply never appeared. It now tells you which skill is
wrong and why.

## Writing one that gets used

- **Describe the trigger.** "Use when laying out a zine spread" beats "Zine
  layout helper" — the first says when to reach for it.
- **Keep `SKILL.md` short.** Push detail into `references/`; the body is loaded
  every time the skill is invoked, the references are not.
- **Say what to read.** The agent sees a file listing, not the contents. If a
  file matters, name it in the instructions.
- **Gate outbound actions.** Anything that sends, posts, spends, or deletes
  should be an executable skill with `requires_approval`.
