# obsidian_read

Read a single note from the user's Obsidian vault by relative path (get
paths from `obsidian_search`; the `.md` suffix is optional). **Strictly
read-only**, with path-traversal and symlink-escape guards — a path can
never resolve outside the vault.

Long notes are truncated at `max_chars` (default 10000).

Vault detection and overrides are shared with `obsidian_search` — see its
SKILL.md.
