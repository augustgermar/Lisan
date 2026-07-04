# gmail_search

Search the user's Gmail with standard Gmail query syntax and return message
metadata as JSON. Read-only (`gmail.readonly` scope).

## First-time setup (user-provisioned credentials)

Credentials are never bundled. Onboard with the shared setup broker:

```bash
lisan skills setup gmail_search -- --check
```

If it prints `NOT_AUTHENTICATED`, follow the broker's next-step hints:

1. The user creates a Google Cloud OAuth client (**Desktop app** type) with the
   **Gmail API** enabled, and downloads `client_secret.json`:
   - Enable the API: https://console.cloud.google.com/apis/library
   - Create the client: https://console.cloud.google.com/apis/credentials
   - If the OAuth app is in Testing, add the user's account under Audience → Test users.
2. `lisan skills setup gmail_search -- --client-secret /path/to/client_secret.json`
3. `lisan skills setup gmail_search -- --auth-url` — send the printed URL to the user.
   After approving, their browser lands on an unreachable `localhost` page
   (expected); they copy the entire address-bar URL.
4. `lisan skills setup gmail_search -- --auth-code 'THE_PASTED_URL'`
5. `lisan skills setup gmail_search -- --check` should print `AUTHENTICATED`.

Tokens live in `~/.local/share/Lisan/credentials/google/` (override with
`LISAN_GOOGLE_CREDENTIALS_DIR` or `skills.google.credentials_dir` in config.json)
and are shared by all gmail_* skills — set up once.

## Usage

Query syntax is the same as the Gmail search box: `is:unread`,
`from:jane@example.com`, `subject:invoice newer_than:7d`, `has:attachment`.

Returns a JSON list of `{id, threadId, from, to, subject, date, snippet, labels}`.
Follow up with `gmail_read` for full bodies.
