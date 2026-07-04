# imessage_recent

List recent iMessage/SMS conversations from the local Messages database,
newest first. Read-only.

## Prerequisites

- `imsg` CLI: `brew install steipete/tap/imsg`
- Full Disk Access for the terminal/app running Lisan (imsg reads
  `~/Library/Messages/chat.db`): System Settings → Privacy & Security →
  Full Disk Access.

Binary override: `LISAN_IMSG_BIN` env var or `skills.imessage.binary` in
config.json.

## Usage

Returns `{chat_id, identifier, name, participants, service, is_group,
last_message_at}` per conversation. Follow up with `imessage_history`
(pass `chat_id`) to read messages.
