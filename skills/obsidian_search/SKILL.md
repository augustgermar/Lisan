# obsidian_search

Search the user's Obsidian vault — note titles and content, case-insensitive.
Returns relative note paths, match counts, and up to three matching-line
snippets per note. **Strictly read-only**: the Obsidian vault is source
material, never a write target (this mirrors Lisan's codex hard write
boundary).

Vault location is auto-detected from Obsidian's own registry
(`~/Library/Application Support/obsidian/obsidian.json`, or the `~/.config`
equivalent on Linux); override with `skills.obsidian.vault_path` in
config.json or the `LISAN_OBSIDIAN_VAULT` environment variable.

Follow up with `obsidian_read` (pass the returned `path`) to read a note.
