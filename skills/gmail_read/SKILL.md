# gmail_read

Read a single Gmail message in full — headers plus best-effort plain-text
body (nested multiparts are walked; HTML is the fallback when no text/plain
part exists). Read-only (`gmail.readonly` scope).

Get message ids from `gmail_search`. Long bodies are truncated at `max_chars`
(default 8000) so tool results stay context-friendly.

Setup is shared with all gmail_* skills — see `gmail_search/SKILL.md` or run:

```bash
lisan skills setup gmail_read -- --check
```
