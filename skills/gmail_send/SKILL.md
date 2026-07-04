# gmail_send

Send email from the user's Gmail account (`gmail.send` scope), or reply to an
existing message with correct threading (`In-Reply-To`/`References` headers
plus the Gmail thread id).

**This skill is approval-gated** (`requires_approval` in schema.json): every
send must be explicitly approved by the user through the active approval
channel — the interactive prompt in CLI chat, or the approve/deny buttons on
Telegram. A denied approval returns a gate message, not an error.

Replying: pass `reply_to_message_id` (a message id from `gmail_search`);
recipient and subject are derived from the original when omitted.

Setup is shared with all gmail_* skills — see `gmail_search/SKILL.md` or run:

```bash
lisan skills setup gmail_send -- --check
```
