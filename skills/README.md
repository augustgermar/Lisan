# skills/ — put your skills here

This folder ships empty on purpose. Skills are procedures *you* write or
install; none of them belong to the project, so nothing here is version
controlled except this file.

Drop a directory in and it is discovered on the next run. There is nothing to
register.

```
skills/
  README.md          <- this file, the only tracked thing in here
  my-skill/
    SKILL.md         <- required
    references/      <- optional, loaded only when SKILL.md points at them
    scripts/         <- optional
```

## The smallest working skill

```markdown
---
name: my-skill
description: Use when <the situation that should trigger this>.
---

# My skill

Step one. Step two.
```

`name` and `description` are the only required fields. This is the same
[Agent Skills](https://code.claude.com/docs/en/skills) format Claude Code,
Codex and other agentic tools use, so a skill written for any of them works
here unchanged.

**The description is the most important line you write.** Only the name and
description stay in the agent's context; the body loads when the skill is
actually invoked, and `references/` files load only if the body sends the
reader to them. That is what makes a large catalogue affordable — so write the
description as *when to use this*, not *what this is*.

## Two kinds

A skill with only `SKILL.md` is **instructional**: the agent loads its
instructions on demand and follows them.

Add `schema.json` and `tool.py` and it becomes **executable**: a callable tool
with JSON-Schema parameters. `tool.py` exposes
`run(args: dict, vault: Path, config: dict) -> str`. Set
`"requires_approval": true` in `schema.json` for anything that leaves the
machine — sending, posting, spending, deleting.

## Commands

```bash
lisan skills validate     # check these against the format, and name any faults
lisan skills migrate      # add frontmatter to skills that predate it
lisan skills list
lisan skills install <name>
```

Full reference: [`docs/skills.md`](../docs/skills.md).

## Where the agent actually loads from

The active set lives in `~/.local/share/Lisan/skills/` (override with
`$LISAN_SKILLS_DIR`). This folder is the local catalogue you install *from* —
`lisan skills install <name>` copies a skill across, along with any shared
`_underscore` library directories it declares.

If you only ever want one copy, putting your skills straight into the active
directory works fine.
