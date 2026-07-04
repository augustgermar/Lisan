# imessage_send

Send an iMessage/SMS via the `imsg` CLI.

**This skill is approval-gated** (`requires_approval` in schema.json): every
send must be explicitly approved by the user through the active approval
channel — the interactive prompt in CLI chat, or the approve/deny buttons on
Telegram.

Pass `to` (phone/email) for direct messages, or `chat_id` from
`imessage_recent` for existing conversations (required for groups).

Prerequisites are shared with all imessage_* skills — see
`imessage_recent/SKILL.md`. Sending additionally requires Automation
permission for Messages.app the first time.
